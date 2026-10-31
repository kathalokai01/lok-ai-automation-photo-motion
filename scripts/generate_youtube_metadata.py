#!/usr/bin/env python3
"""Generate accurate, story-aware YouTube metadata."""

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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_and_validate, get_format, get_topic

INPUT_TOPIC = ROOT / "Input" / "topic.txt"
INPUT_STORY = ROOT / "output" / "story" / "ai_story.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

OUTPUT_DIR = ROOT / "output" / "publish"
OUTPUT_JSON = OUTPUT_DIR / "youtube_metadata.json"
OUTPUT_TITLE = OUTPUT_DIR / "youtube_title.txt"
OUTPUT_DESCRIPTION = OUTPUT_DIR / "youtube_description.txt"
OUTPUT_TAGS = OUTPUT_DIR / "youtube_tags.txt"
OUTPUT_TEXT = ROOT / "output" / "youtube_metadata.txt"

VERSION = "youtube-metadata-v6-fixed-story-aware"
MAX_RETRIES = 3
REQUEST_TIMEOUT = 180
MAX_TAGS = 20
MIN_TAGS = 5


def log(*values):
    print(*values, flush=True)


def clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def read_json(path: Path) -> dict:
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


def atomic_write_json(path: Path, data: dict):
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def load_config():
    if not INPUT_TOPIC.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(INPUT_TOPIC)

    if not isinstance(config, dict):
        raise RuntimeError("Invalid input configuration.")

    topic = clean(get_topic(config))
    mode = clean(get_format(config)).lower()

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode!r}")

    return config, topic, mode


def load_selected_model():
    data = read_json(MODEL_FILE)

    if data.get("status") != "selected":
        raise RuntimeError("Selected Gemini model is not confirmed.")

    model = clean(data.get("model"))

    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(f"Invalid selected model: {model!r}")

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
    """Read the complete story in its original order."""

    for key in (
        "story_text",
        "full_story",
        "original_story",
    ):
        value = clean(story.get(key))
        if len(value) >= 100:
            return value, key

    sections = []

    for part_number, part in enumerate(ordered_parts(story), 1):
        scenes = part.get("scenes")

        if not isinstance(scenes, list):
            continue

        indexed = []

        for index, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                continue

            try:
                scene_number = int(
                    scene.get("scene", scene.get("local_scene", index))
                )
            except (TypeError, ValueError):
                scene_number = index

            indexed.append((scene_number, scene))

        indexed.sort(key=lambda item: item[0])

        for scene_number, scene in indexed:
            narration = clean(
                scene.get("narration")
                or scene.get("narration_text")
                or scene.get("text")
            )

            if narration:
                sections.append(narration)

    reconstructed = "\n\n".join(sections).strip()

    if len(reconstructed) >= 100:
        return reconstructed, "ordered_parts_and_scenes"

    for key in ("story", "content", "narration", "script"):
        value = clean(story.get(key))
        if len(value) >= 100:
            return value, key

    raise RuntimeError(
        "Complete story text not found in ai_story.json. "
        "Metadata generation stopped to prevent invented metadata."
    )


def validate_story_status(story):
    status = clean(story.get("status")).lower()

    if status not in {"completed", "complete", "success"}:
        raise RuntimeError(
            f"Story is not completed. Found status={status!r}."
        )

    if story.get("error"):
        raise RuntimeError("Story contains an error field.")


def prepare_story(story):
    validate_story_status(story)

    story_text, source = extract_canonical_story(story)

    existing_title = clean(
        story.get("final_title")
        or story.get("generated_title")
        or story.get("story_title")
        or story.get("title")
    )

    words = len(story_text.split())

    if words < 30:
        raise RuntimeError(
            f"Story is too short for metadata: {words} words."
        )

    return {
        "text": story_text,
        "source": source,
        "existing_title": existing_title,
        "character_count": len(story_text),
        "word_count": words,
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
            f"Only {len(tags)} valid tags; minimum is {MIN_TAGS}."
        )

    return tags


def validate_metadata(data, topic, model, fingerprint, mode):
    if not isinstance(data, dict):
        raise RuntimeError("Metadata response is not an object.")

    title = clean(data.get("title"))
    description = clean(data.get("description"))
    tags = normalize_tags(data.get("tags"))

    if not title:
        raise RuntimeError("Generated title is empty.")

    if not description:
        raise RuntimeError("Generated description is empty.")

    if len(title) > 100:
        raise RuntimeError(
            f"Title exceeds YouTube title limit: {len(title)} characters."
        )

    if len(description) > 5000:
        description = description[:4997].rstrip() + "..."

    return {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "topic": topic,
        "model": model,
        "title": title,
        "description": description,
        "tags": tags,
        "input_fingerprint": fingerprint,
    }


def make_fingerprint(topic, story, model, mode, config):
    payload = {
        "version": VERSION,
        "topic": topic,
        "story_text": story["text"],
        "existing_title": story["existing_title"],
        "story_source": story["source"],
        "model": model,
        "format": mode,
        "config": config,
    }

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def load_cache(fingerprint, topic, model, mode):
    if not OUTPUT_JSON.is_file():
        return None

    try:
        data = read_json(OUTPUT_JSON)

        if data.get("status") != "completed":
            return None

        if data.get("version") != VERSION:
            return None

        if data.get("input_fingerprint") != fingerprint:
            return None

        if data.get("model") != model:
            return None

        if data.get("format") != mode:
            return None

        return validate_metadata(
            data,
            topic,
            model,
            fingerprint,
            mode,
        )

    except Exception as exc:
        log("Metadata cache rejected:", exc)
        return None


def extract_response_text(result):
    candidates = result.get("candidates")

    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("Gemini returned no metadata candidates.")

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
            f"Gemini returned empty metadata. Reason={finish_reason}"
        )

    return response

def generate_metadata(
    api_key,
    model,
    topic,
    story,
    mode,
    fingerprint,
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
            "This is a YouTube Short. Create a concise, "
            "high-impact Hindi title and a short-video description. "
            "Do not describe it as a full-length video."
        )
    else:
        format_instruction = (
            "This is a full-length story video. Create a compelling "
            "title and a detailed description of the complete story."
        )

    prompt = f"""
You are a professional Hindi YouTube metadata editor.

Read and understand the COMPLETE story before writing metadata.

TOPIC:
{topic or "Not supplied"}

EXISTING TITLE:
{story["existing_title"] or "Not supplied"}

VIDEO FORMAT:
{mode}

FORMAT REQUIREMENT:
{format_instruction}

COMPLETE STORY:
<complete_story>
{story["text"]}
</complete_story>

TASK:
1. Understand the complete plot, characters, conflict, turning point,
   important events and actual ending.
2. Create a compelling Hindi title based on the whole story.
3. Do not choose a title merely from the opening sentence.
4. Do not blindly copy the topic if the story suggests a better title.
5. Do not invent characters, events, locations or twists.
6. Use suspense only when supported by the actual story.
7. Write an accurate Hindi description of the actual story.
8. Mention the correct video format naturally.
9. Generate 10 to 20 relevant Hindi/English search tags.
10. Tags must relate directly to the story, characters, genre or theme.
11. Do not use unrelated trending keywords or keyword stuffing.
12. Do not fabricate links, claims, credits or hashtags.
13. Keep title readable and natural, not clickbait.
14. Return only the JSON object matching the response schema.
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{urllib.parse.quote(model, safe='-._')}:generateContent"
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
            data=json.dumps(
                payload,
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )

        try:
            log(
                f"Gemini metadata attempt {attempt}/{MAX_RETRIES}; "
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

            return validate_metadata(
                parsed,
                topic,
                model,
                fingerprint,
                mode,
            )

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )[:2000]

            last_error = RuntimeError(
                f"Gemini HTTP {exc.code}: {body}"
            )

            if exc.code in (400, 401, 403, 404):
                break

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
    title = clean(metadata.get("title"))
    description = clean(metadata.get("description"))
    tags = normalize_tags(metadata.get("tags"))

    if not title or not description:
        raise RuntimeError("Cannot write incomplete metadata.")

    return (
        f"TITLE\n{title}\n\n"
        f"DESCRIPTION\n{description}\n\n"
        f"TAGS\n{', '.join(tags)}\n"
    )


def save_outputs(metadata):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    validated = validate_metadata(
        metadata,
        metadata["topic"],
        metadata["model"],
        metadata["input_fingerprint"],
        metadata["format"],
    )

    combined = build_combined_text(validated)

    atomic_write_json(OUTPUT_JSON, validated)
    atomic_write(OUTPUT_TITLE, validated["title"] + "\n")
    atomic_write(
        OUTPUT_DESCRIPTION,
        validated["description"] + "\n",
    )
    atomic_write(
        OUTPUT_TAGS,
        ", ".join(validated["tags"]) + "\n",
    )
    atomic_write(OUTPUT_TEXT, combined)

    saved_json = read_json(OUTPUT_JSON)

    verified = validate_metadata(
        saved_json,
        validated["topic"],
        validated["model"],
        validated["input_fingerprint"],
        validated["format"],
    )

    if verified != validated:
        raise RuntimeError("Saved metadata JSON verification failed.")

    if OUTPUT_TITLE.read_text(encoding="utf-8").strip() != validated["title"]:
        raise RuntimeError("Saved title verification failed.")

    if (
        OUTPUT_DESCRIPTION.read_text(encoding="utf-8").strip()
        != validated["description"]
    ):
        raise RuntimeError("Saved description verification failed.")

    saved_tags = normalize_tags(
        OUTPUT_TAGS.read_text(encoding="utf-8").split(",")
    )

    if saved_tags != validated["tags"]:
        raise RuntimeError("Saved tags verification failed.")

    saved_text = OUTPUT_TEXT.read_text(encoding="utf-8")

    if validated["title"] not in saved_text:
        raise RuntimeError("Combined metadata title verification failed.")

    if validated["description"] not in saved_text:
        raise RuntimeError(
            "Combined metadata description verification failed."
        )

    log("Metadata files saved and verified.")


def main():
    config, topic, mode = load_config()

    story_data = read_json(INPUT_STORY)
    prepared = prepare_story(story_data)

    model = load_selected_model()

    fingerprint = make_fingerprint(
        topic=topic,
        story=prepared,
        model=model,
        mode=mode,
        config=config,
    )

    cached = load_cache(
        fingerprint,
        topic,
        model,
        mode,
    )

    if cached is not None:
        metadata = cached
        log("Using validated metadata cache; Gemini API call skipped.")

    else:
        api_key = (
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )

        if not api_key:
            raise RuntimeError(
                "No Gemini API key and no matching metadata cache."
            )

        log("Generating metadata from the complete story.")
        log("Selected model:", model)
        log("Story source:", prepared["source"])
        log("Story words:", prepared["word_count"])

        metadata = generate_metadata(
            api_key=api_key,
            model=model,
            topic=topic,
            story=prepared,
            mode=mode,
            fingerprint=fingerprint,
        )

    save_outputs(metadata)

    log("YOUTUBE METADATA SUCCESS")
    log("Format:", mode)
    log("Title:", metadata["title"])
    log("Tags:", len(metadata["tags"]))
    log("JSON:", OUTPUT_JSON)
    log("Title file:", OUTPUT_TITLE)
    log("Description file:", OUTPUT_DESCRIPTION)
    log("Tags file:", OUTPUT_TAGS)
    log("Combined file:", OUTPUT_TEXT)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("YOUTUBE METADATA FAILED:", exc)
        sys.exit(1)