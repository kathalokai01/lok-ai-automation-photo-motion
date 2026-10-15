
#!/usr/bin/env python3
"""Generate concise, story-specific YouTube metadata with safe caching."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INPUT_TOPIC = ROOT / "Input" / "topic.txt"
INPUT_STORY = ROOT / "output" / "story" / "ai_story.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

OUTPUT_DIR = ROOT / "output" / "publish"
OUTPUT_JSON = OUTPUT_DIR / "youtube_metadata.json"
OUTPUT_TITLE = OUTPUT_DIR / "youtube_title.txt"
OUTPUT_DESCRIPTION = OUTPUT_DIR / "youtube_description.txt"
OUTPUT_TAGS = OUTPUT_DIR / "youtube_tags.txt"
CHECKPOINT = OUTPUT_DIR / "metadata_checkpoint.json"

# Required by the final-output validator and packaging pipeline.
PACKAGE_METADATA = ROOT / "output" / "youtube_metadata.txt"

MAX_RETRIES = 3
MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 1000
MAX_TAGS = 20
VERSION = "metadata-v3-validator-compatible"


def log(message):
    print(message, flush=True)


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required file missing: {path.relative_to(ROOT)}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {path.name}: {exc}") from exc

    if not isinstance(data, (dict, list, str)):
        raise RuntimeError(f"Unexpected JSON structure: {path.name}")

    return data


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def clean_text(value, limit=None):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit is not None:
        value = value[:limit].rstrip()
    return value


def read_topic():
    if not INPUT_TOPIC.is_file():
        raise RuntimeError(f"Missing input file: {INPUT_TOPIC.relative_to(ROOT)}")

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
            "AUDIENCE",
        }:
            values[key] = value.strip().strip("\"'")

    topic = clean_text(values.get("TOPIC") or values.get("TITLE"))

    if not topic:
        raise RuntimeError("Set TOPIC or TITLE in Input/topic.txt")

    return values, topic


def load_model():
    if not MODEL_FILE.is_file():
        log("Selected-model file missing; metadata will use local fallback.")
        return ""

    try:
        data = json.loads(MODEL_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"Invalid model configuration; using local fallback: {exc}")
        return ""

    if not isinstance(data, dict):
        return ""

    model = clean_text(data.get("model"))

    if data.get("status") not in (None, "", "selected"):
        log("Model is not marked selected; using local fallback.")
        return ""

    return model


def story_text(story):
    """Extract useful context without exposing all story data in metadata."""
    preferred = (
        "title",
        "topic",
        "summary",
        "story",
        "full_story",
        "narration",
        "text",
        "content",
        "parts",
        "scenes",
    )

    chunks = []

    def walk(value, depth=0):
        if depth > 5 or len(chunks) >= 80:
            return

        if isinstance(value, str):
            text = clean_text(value)
            if text:
                chunks.append(text[:2500])
            return

        if isinstance(value, list):
            for item in value[:40]:
                walk(item, depth + 1)
            return

        if isinstance(value, dict):
            for key in preferred:
                if key in value:
                    walk(value[key], depth + 1)

    walk(story)
    return "\n".join(chunks)[:18000]


def canonical_json(data):
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def fingerprint(topic_values, topic, story, model):
    payload = {
        "version": VERSION,
        "topic_values": topic_values,
        "topic": topic,
        "story": story,
        "model": model,
    }
    return hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()


def normalize_metadata(data, topic, language):
    if not isinstance(data, dict):
        raise ValueError("Metadata response must be a JSON object")

    title = clean_text(data.get("title"), MAX_TITLE_LENGTH)
    description = clean_text(
        data.get("description"), MAX_DESCRIPTION_LENGTH
    )
    raw_tags = data.get("tags", [])

    if not title:
        title = clean_text(topic, MAX_TITLE_LENGTH)

    if not description:
        description = f"{title} — कहानी और सिनेमैटिक कहानी-कथन।"

    if not isinstance(raw_tags, list):
        raw_tags = []

    tags = []
    seen = set()

    for item in raw_tags:
        tag = clean_text(item, 100).strip(" ,#")
        normalized = tag.casefold()

        if tag and normalized not in seen:
            seen.add(normalized)
            tags.append(tag)

        if len(tags) >= MAX_TAGS:
            break

    if not tags:
        tags = [
            topic,
            "Hindi story",
            "Hindi kahani",
            "हिंदी कहानी",
        ]

    return {
        "title": title,
        "description": description,
        "tags": tags[:MAX_TAGS],
        "language": clean_text(language, 40) or "Hindi",
    }


def local_fallback(topic, story, language):
    """Create useful metadata without spending an API request."""
    context = story_text(story).casefold()
    title = clean_text(topic, MAX_TITLE_LENGTH)

    # Do not reveal story events or endings in the description.
    description = (
        f"{title} — एक सिनेमैटिक कहानी, "
        "जिसमें कहानी-कथन और रहस्य का अनुभव मिलेगा।"
    )

    tags = [
        title,
        "Hindi story",
        "Hindi kahani",
        "हिंदी कहानी",
        "Hindi storytelling",
        "cinematic story",
    ]

    topic_context = (title + " " + context).casefold()

    topic_tags = [
        (("mystery", "रहस्य", "rahasya"), ["mystery story", "रहस्य कहानी"]),
        (("horror", "horrifying", "भूत", "डरावनी", "हॉरर"), ["horror story", "डरावनी कहानी"]),
        (("love", "romance", "प्रेम", "प्यार"), ["love story", "प्रेम कहानी"]),
        (("crime", "criminal", "अपराध", "जुर्म"), ["crime story", "अपराध कहानी"]),
        (("inspiration", "motivational", "प्रेरणा", "प्रेरणादायक"), ["inspirational story", "प्रेरणादायक कहानी"]),
        (("thriller", "suspense", "सस्पेंस", "रोमांच"), ["suspense story", "thriller kahani"]),
        (("historical", "history", "इतिहास", "ऐतिहासिक"), ["historical story", "ऐतिहासिक कहानी"]),
        (("comedy", "funny", "हास्य", "मजेदार"), ["comedy story", "हास्य कहानी"]),
    ]

    for keywords, additions in topic_tags:
        if any(word in topic_context for word in keywords):
            tags.extend(additions)

    return normalize_metadata(
        {"title": title, "description": description, "tags": tags},
        topic,
        language,
    )


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
You create accurate YouTube metadata for a Hindi cinematic storytelling channel.

TOPIC:
{topic}

STORY CONTEXT:
{story_text(story)}

LANGUAGE:
{language or "Hindi"}

Rules:
- Make one engaging, truthful title, maximum 100 characters.
- Use natural Hindi or Hinglish suited to the topic.
- Do not invent events, claims, real people, or outcomes.
- Description: 2-3 short sentences, maximum 1000 characters.
- Do not reveal the plot, major twists, or ending.
- Do not paste the story or narration into the description.
- Do not include URLs, generic keyword stuffing, or unrelated tags.
- Return 8-15 relevant tags when possible.
- No subscribe/like requests.
- Return only JSON matching the supplied schema.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0.35,
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
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )

        try:
            log(f"Generating metadata (attempt {attempt}/{MAX_RETRIES})")

            with urllib.request.urlopen(request, timeout=75) as response:
                result = json.loads(response.read().decode("utf-8"))

            candidates = result.get("candidates", [])
            if not candidates:
                raise ValueError("API returned no candidates")

            parts = candidates[0].get("content", {}).get("parts", [])
            response_text = next(
                (
                    item.get("text", "")
                    for item in parts
                    if isinstance(item, dict) and item.get("text")
                ),
                "",
            )

            if not response_text:
                raise ValueError("API response contained no text")

            parsed = json.loads(response_text)
            return normalize_metadata(parsed, topic, language)

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
            log(f"Metadata attempt failed: {exc}")

            if isinstance(exc, urllib.error.HTTPError):
                if exc.code in (400, 401, 403, 404):
                    break

            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2)

    raise RuntimeError(f"Metadata API failed: {last_error}")


def package_text(metadata):
    """Plain-text deliverable consumed by final validation and packaging."""
    return (
        "TITLE\n"
        f"{metadata['title']}\n\n"
        "DESCRIPTION\n"
        f"{metadata['description']}\n\n"
        "TAGS\n"
        f"{', '.join(metadata['tags'])}\n\n"
        f"LANGUAGE\n{metadata['language']}\n"
    )


def save_outputs(metadata, input_fingerprint, source):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PACKAGE_METADATA.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "status": "completed",
        "version": VERSION,
        "source": source,
        "input_fingerprint": input_fingerprint,
        **metadata,
    }

    write_json(OUTPUT_JSON, result)

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

    # Write atomically so a failed job cannot leave a partial package file.
    temporary = PACKAGE_METADATA.with_suffix(".txt.tmp")
    temporary.write_text(package_text(metadata), encoding="utf-8")
    temporary.replace(PACKAGE_METADATA)

    write_json(CHECKPOINT, {
        "status": "completed",
        "version": VERSION,
        "input_fingerprint": input_fingerprint,
        "source": source,
    })


def load_cached_metadata(expected_fingerprint):
    if not OUTPUT_JSON.is_file():
        return None

    try:
        data = json.loads(OUTPUT_JSON.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    if data.get("status") != "completed":
        return None

    if data.get("version") != VERSION:
        return None

    if data.get("input_fingerprint") != expected_fingerprint:
        return None

    try:
        metadata = normalize_metadata(data, data.get("title", ""), data.get("language", "Hindi"))
    except (ValueError, TypeError):
        return None

    required_files = (OUTPUT_TITLE, OUTPUT_DESCRIPTION, OUTPUT_TAGS)
    if not all(path.is_file() for path in required_files):
        return None

    # Verify the package-facing output too; regenerate if it was deleted.
    if not PACKAGE_METADATA.is_file():
        return metadata

    return metadata


def main():
    topic_values, topic = read_topic()
    story = read_json(INPUT_STORY)
    model = load_model()

    language = (
        topic_values.get("LANGUAGE")
        or topic_values.get("CAPTIONS")
        or "Hindi"
    )

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    input_fingerprint = fingerprint(topic_values, topic, story, model)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cached = load_cached_metadata(input_fingerprint)
    if cached:
        log("Matching metadata checkpoint found; skipping API request.")
        save_outputs(cached, input_fingerprint, "cache")
        log(f"Title: {cached['title']}")
        log("Metadata reused without another API request.")
        return

    if not api_key or not model:
        log("Gemini key or selected model unavailable; using local fallback.")
        metadata = local_fallback(topic, story, language)
        source = "local_fallback"
    else:
        try:
            metadata = generate_metadata(
                api_key, model, topic, story, language
            )
            source = "gemini"
        except RuntimeError as exc:
            log(f"WARNING: {exc}")
            log("Using local fallback; no further API requests.")
            metadata = local_fallback(topic, story, language)
            source = "local_fallback"

    save_outputs(metadata, input_fingerprint, source)

    log("======================================")
    log("YOUTUBE METADATA READY")
    log(f"Source: {source}")
    log(f"Title: {metadata['title']}")
    log(f"Tags: {len(metadata['tags'])}")
    log(f"Package file: {PACKAGE_METADATA.relative_to(ROOT)}")
    log("======================================")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        raise SystemExit(1)
