#!/usr/bin/env python3
"""Generate topic-specific YouTube metadata with resumable checkpoints."""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

INPUT_TOPIC = Path("Input/topic.txt")
INPUT_STORY = Path("output/story/ai_story.json")
MODEL_FILE = Path("output/config/selected_model.json")

OUTPUT_DIR = Path("output/publish")
OUTPUT_JSON = OUTPUT_DIR / "youtube_metadata.json"
OUTPUT_TITLE = OUTPUT_DIR / "youtube_title.txt"
OUTPUT_DESCRIPTION = OUTPUT_DIR / "youtube_description.txt"
OUTPUT_TAGS = OUTPUT_DIR / "youtube_tags.txt"
CHECKPOINT = OUTPUT_DIR / "metadata_checkpoint.json"

MAX_RETRIES = 3
MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 1000
MAX_TAGS = 20


def read_json(path):
    if not path.is_file():
        raise SystemExit(f"ERROR: Required file missing: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: Cannot read {path}: {exc}")


def read_topic():
    if not INPUT_TOPIC.is_file():
        raise SystemExit(f"ERROR: Missing {INPUT_TOPIC}")

    values = {}

    for raw_line in INPUT_TOPIC.read_text(
        encoding="utf-8-sig"
    ).splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().upper()

        if key in {
            "TOPIC",
            "TITLE",
            "FORMAT",
            "LANGUAGE",
            "CAPTIONS",
        }:
            values[key] = value.strip().strip("\"'")

    topic = values.get("TOPIC", "").strip()
    title = values.get("TITLE", "").strip()

    if not topic and not title:
        raise SystemExit(
            "ERROR: Set TOPIC or TITLE in Input/topic.txt"
        )

    return values, topic or title


def load_model():
    if not MODEL_FILE.is_file():
        raise SystemExit(
            f"ERROR: Selected-model configuration missing: {MODEL_FILE}"
        )

    try:
        config = json.loads(
            MODEL_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: Invalid model configuration: {exc}")

    model = str(config.get("model", "")).strip()

    if not model:
        raise SystemExit(
            "ERROR: No model configured in selected_model.json"
        )

    if config.get("status") not in (None, "", "selected"):
        raise SystemExit(
            "ERROR: Selected-model configuration is not marked selected"
        )

    return model


def canonical_json(data):
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def fingerprint(topic_values, topic, story, model):
    payload = {
        "topic_values": topic_values,
        "topic": topic,
        "story": story,
        "model": model,
        "version": "metadata-v2",
    }
    return hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()


def clean_text(value, limit=None):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit is not None:
        text = text[:limit].rstrip()
    return text


def story_text(story):
    """Extract useful story text without dumping all internal metadata."""
    if isinstance(story, str):
        return story[:30000]

    if isinstance(story, list):
        return "\n".join(
            story_text(item) for item in story
        )[:30000]

    if isinstance(story, dict):
        preferred = (
            "title",
            "topic",
            "story",
            "full_story",
            "narration",
            "text",
            "content",
            "summary",
            "parts",
            "scenes",
        )

        chunks = []
        for key in preferred:
            if key in story:
                value = story[key]
                if isinstance(value, (str, list, dict)):
                    rendered = story_text(value)
                    if rendered:
                        chunks.append(f"{key}: {rendered}")

        if chunks:
            return "\n".join(chunks)[:30000]

        return json.dumps(
            story, ensure_ascii=False
        )[:30000]

    return str(story)


def local_fallback(topic, story, language):
    """Create usable metadata without an extra external API call."""
    title = clean_text(topic, MAX_TITLE_LENGTH)
    content = clean_text(story_text(story), 700)

    if content and content.lower() != title.lower():
        description = (
            f"{title} — एक कहानी। "
            f"{content[:500]}"
        )
    else:
        description = (
            f"{title} — हिंदी कहानी और सिनेमैटिक कहानी-कथन।"
        )

    tags = [
        title,
        "Hindi story",
        "Hindi kahani",
        "हिंदी कहानी",
        "Hindi storytelling",
        "cinematic story",
    ]

    if any(word in (title + " " + content).lower()
           for word in ("mystery", "rahasya", "रहस्य", "रहस्यमय")):
        tags.extend(["mystery story", "रहस्य कहानी"])

    if any(word in (title + " " + content).lower()
           for word in ("horror", " डरावनी", "भूत", "हॉरर")):
        tags.extend(["horror story", "डरावनी कहानी"])

    return normalize_metadata(
        {
            "title": title,
            "description": description,
            "tags": tags,
        },
        topic,
        language,
    )


def normalize_metadata(data, topic, language):
    if not isinstance(data, dict):
        raise ValueError("Metadata response must be a JSON object")

    title = clean_text(data.get("title"), MAX_TITLE_LENGTH)
    description = clean_text(
        data.get("description"), MAX_DESCRIPTION_LENGTH
    )
    tags = data.get("tags", [])

    if not title:
        title = clean_text(topic, MAX_TITLE_LENGTH)

    if not description:
        description = f"{title} — कहानी और सिनेमैटिक कहानी-कथन।"

    if not isinstance(tags, list):
        tags = []

    normalized_tags = []
    seen = set()

    for tag in tags:
        tag = clean_text(tag, 100).strip(" ,#")
        key = tag.casefold()

        if tag and key not in seen:
            seen.add(key)
            normalized_tags.append(tag)

        if len(normalized_tags) >= MAX_TAGS:
            break

    if not normalized_tags:
        normalized_tags = [
            topic,
            "Hindi story",
            "Hindi storytelling",
            "हिंदी कहानी",
        ]

    return {
        "title": title,
        "description": description,
        "tags": normalized_tags[:MAX_TAGS],
        "language": language,
    }


def generate_metadata(api_key, model, topic, story, language):
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

    prompt = f"""
You create honest YouTube metadata for a Hindi cinematic
storytelling channel.

TOPIC:
{topic}

STORY CONTENT:
{story_text(story)}

LANGUAGE PREFERENCE:
{language or "Hindi"}

Rules:
- Produce one accurate, interesting title, maximum 100 characters.
- Use natural Hindi or Hinglish suited to the topic.
- Do not invent events, claims, real people, or outcomes.
- Description must be brief, around 2-4 sentences, maximum 1000 characters.
- Do not reveal the entire story or its ending.
- Do not include URLs, keyword stuffing, or unrelated tags.
- Return 10-15 relevant tags when appropriate.
- Do not add subscribe/like requests.
- Return only the JSON matching the schema.
"""

    payload = {
        "contents": [
            {"parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0.4,
        },
    }

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
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
            print(
                f"Generating metadata with configured model "
                f"(attempt {attempt}/{MAX_RETRIES})"
            )

            with urllib.request.urlopen(
                request, timeout=90
            ) as response:
                result = json.loads(
                    response.read().decode("utf-8")
                )

            candidates = result.get("candidates", [])
            if not candidates:
                raise ValueError("API returned no candidates")

            parts = candidates[0].get(
                "content", {}
            ).get("parts", [])

            response_text = next(
                (
                    part["text"]
                    for part in parts
                    if isinstance(part, dict) and part.get("text")
                ),
                "",
            )

            if not response_text:
                raise ValueError("API response contained no text")

            data = json.loads(response_text)
            return normalize_metadata(data, topic, language)

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            KeyError,
            IndexError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            print(f"Metadata attempt failed: {exc}")

            if isinstance(exc, urllib.error.HTTPError):
                if exc.code in (400, 401, 403, 404):
                    break

            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2)

    raise RuntimeError(
        f"Metadata API failed after retries: {last_error}"
    )


def save_outputs(metadata, fingerprint_value, source):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    result = {
        "status": "completed",
        "version": "metadata-v2",
        "source": source,
        "input_fingerprint": fingerprint_value,
        **metadata,
    }

    temporary = OUTPUT_JSON.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(OUTPUT_JSON)

    OUTPUT_TITLE.write_text(
        metadata["title"] + "\n", encoding="utf-8"
    )
    OUTPUT_DESCRIPTION.write_text(
        metadata["description"] + "\n", encoding="utf-8"
    )
    OUTPUT_TAGS.write_text(
        ", ".join(metadata["tags"]) + "\n", encoding="utf-8"
    )

    CHECKPOINT.write_text(
        json.dumps(
            {
                "status": "completed",
                "input_fingerprint": fingerprint_value,
                "source": source,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def load_cached_metadata(expected_fingerprint):
    if not OUTPUT_JSON.is_file():
        return None

    try:
        data = json.loads(
            OUTPUT_JSON.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None

    if data.get("status") != "completed":
        return None

    if data.get("input_fingerprint") != expected_fingerprint:
        return None

    title = clean_text(data.get("title"), MAX_TITLE_LENGTH)
    description = clean_text(
        data.get("description"), MAX_DESCRIPTION_LENGTH
    )
    tags = data.get("tags")

    if not title or not description or not isinstance(tags, list):
        return None

    if not all(
        path.is_file()
        for path in (
            OUTPUT_TITLE,
            OUTPUT_DESCRIPTION,
            OUTPUT_TAGS,
        )
    ):
        return None

    return {
        "title": title,
        "description": description,
        "tags": tags[:MAX_TAGS],
        "language": data.get("language", "Hindi"),
    }


def main():
    topic_values, topic = read_topic()
    story = read_json(INPUT_STORY)
    model = load_model()

    language = (
        topic_values.get("LANGUAGE")
        or topic_values.get("CAPTIONS")
        or "Hindi"
    )

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    input_hash = fingerprint(
        topic_values, topic, story, model
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cached = load_cached_metadata(input_hash)
    if cached:
        print("Matching metadata checkpoint found; skipping API call.")
        # Recreate the JSON/checkpoint only if it was otherwise removed.
        save_outputs(cached, input_hash, "cache")
        print(f"Title: {cached['title']}")
        print("Metadata reused; no Gemini request made.")
        return

    if not key:
        print(
            "GEMINI_API_KEY is unavailable; using local metadata fallback."
        )
        metadata = local_fallback(topic, story, language)
        source = "local_fallback"
    else:
        try:
            metadata = generate_metadata(
                key, model, topic, story, language
            )
            source = "gemini"
        except RuntimeError as exc:
            print(f"WARNING: {exc}")
            print("Using local fallback; no further API calls.")
            metadata = local_fallback(topic, story, language)
            source = "local_fallback"

    save_outputs(metadata, input_hash, source)

    print("======================================")
    print("YOUTUBE METADATA READY")
    print(f"Source: {source}")
    print(f"Title: {metadata['title']}")
    print(f"Tags: {len(metadata['tags'])}")
    print(f"JSON: {OUTPUT_JSON}")
    print(f"Checkpoint: {CHECKPOINT}")
    print("======================================")


if __name__ == "__main__":
    main()