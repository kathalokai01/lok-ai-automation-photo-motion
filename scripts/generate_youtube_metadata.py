#!/usr/bin/env python3
"""Story-aware YouTube metadata with title-from-story fallback and caching."""

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
FINAL_TITLE = ROOT / "output" / "story" / "final_title.txt"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

OUTPUT_DIR = ROOT / "output" / "publish"
OUTPUT_JSON = OUTPUT_DIR / "youtube_metadata.json"
OUTPUT_TITLE = OUTPUT_DIR / "youtube_title.txt"
OUTPUT_DESCRIPTION = OUTPUT_DIR / "youtube_description.txt"
OUTPUT_TAGS = OUTPUT_DIR / "youtube_tags.txt"
CHECKPOINT = OUTPUT_DIR / "metadata_checkpoint.json"
PACKAGE_METADATA = ROOT / "output" / "youtube_metadata.txt"

VERSION = "metadata-v4-story-title-fallback"
MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 1000
MAX_TAGS = 20
MAX_RETRIES = 2


def log(message):
    print(message, flush=True)


def clean(value, limit=None):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit is not None:
        value = value[:limit].rstrip()
    return value


def read_json(path, required=False):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path.relative_to(ROOT)}")
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON in {path.name}: {exc}") from exc
        log(f"WARNING: Cannot read {path.name}: {exc}")
        return {}

    if not isinstance(data, (dict, list)):
        return {}
    return data


def read_input():
    if not INPUT_TOPIC.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    values = {}
    for raw in INPUT_TOPIC.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().upper()] = value.strip().strip("\"'")

    return values


def story_strings(value, depth=0):
    """Extract story text without recursively dumping arbitrary metadata."""
    if depth > 6:
        return []

    if isinstance(value, str):
        text = clean(value)
        return [text] if text else []

    if isinstance(value, list):
        result = []
        for item in value[:60]:
            result.extend(story_strings(item, depth + 1))
        return result

    if isinstance(value, dict):
        result = []
        preferred = (
            "title", "topic", "summary", "logline", "story",
            "full_story", "story_text", "narration", "text",
            "content", "parts", "scenes",
        )
        for key in preferred:
            if key in value:
                result.extend(story_strings(value[key], depth + 1))
        return result

    return []


def read_story_title(story):
    """Prefer explicit generated titles; never use a scene's first sentence."""
    if FINAL_TITLE.is_file():
        try:
            for line in FINAL_TITLE.read_text(
                encoding="utf-8-sig"
            ).splitlines():
                candidate = clean(line, MAX_TITLE_LENGTH)
                if candidate and not candidate.startswith(("#", "[")):
                    return candidate
        except OSError:
            pass

    if isinstance(story, dict):
        for key in ("title", "story_title", "generated_title"):
            candidate = clean(story.get(key), MAX_TITLE_LENGTH)
            if candidate:
                return candidate

        for container_key in ("metadata", "story_metadata"):
            container = story.get(container_key)
            if isinstance(container, dict):
                for key in ("title", "story_title"):
                    candidate = clean(container.get(key), MAX_TITLE_LENGTH)
                    if candidate:
                        return candidate

    return ""


def derive_title(topic, story):
    """Derive a conservative title from explicit story context if needed."""
    title = clean(topic, MAX_TITLE_LENGTH)
    if title:
        return title

    title = read_story_title(story)
    if title:
        return title

    if isinstance(story, dict):
        for key in ("summary", "logline", "story", "full_story", "story_text"):
            candidate = clean(story.get(key))
            if candidate:
                # Use a meaningful opening phrase, not a whole narration line.
                sentence = re.split(r"(?<=[.!?।])\s+", candidate, maxsplit=1)[0]
                sentence = clean(sentence, 68).rstrip(" ,;:-")
                if len(sentence) >= 8:
                    return clean(f"कहानी: {sentence}", MAX_TITLE_LENGTH)

        parts = story.get("parts", [])
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                for key in ("summary", "title", "story", "text"):
                    candidate = clean(part.get(key))
                    if candidate and key in ("summary", "title"):
                        return clean(candidate, MAX_TITLE_LENGTH)

    return "हिंदी कहानी"


def load_model():
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict):
        return ""

    if data.get("status") not in (None, "", "selected"):
        return ""

    return clean(data.get("model"), 150)


def canonical(data):
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def fingerprint(values, story, model, title):
    payload = {
        "version": VERSION,
        "input": values,
        "story": story,
        "model": model,
        "title": title,
    }
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def infer_tags(title, context):
    text = f"{title} {context}".casefold()
    tags = [title, "Hindi story", "Hindi kahani", "हिंदी कहानी",
            "Hindi storytelling", "cinematic story"]

    groups = [
        (("रहस्य", "mystery", "secret", "गुमशुदा"),
         ["mystery story", "रहस्य कहानी"]),
        (("भूत", "डरावनी", "हॉरर", "horror", "ghost", "haunted"),
         ["horror story", "डरावनी कहानी"]),
        (("प्रेम", "प्यार", "love", "romance"),
         ["love story", "प्रेम कहानी"]),
        (("अपराध", "जुर्म", "crime", "criminal"),
         ["crime story", "अपराध कहानी"]),
        (("प्रेरणा", "प्रेरणादायक", "motivational", "inspiration"),
         ["inspirational story", "प्रेरणादायक कहानी"]),
        (("सस्पेंस", "रोमांच", "thriller", "suspense"),
         ["suspense story", "thriller kahani"]),
        (("इतिहास", "ऐतिहासिक", "historical", "king", "राजा"),
         ["historical story", "ऐतिहासिक कहानी"]),
        (("हास्य", "मजेदार", "comedy", "funny"),
         ["comedy story", "हास्य कहानी"]),
        (("माँ", "मां", "पिता", "परिवार", "mother", "father", "family"),
         ["family story", "परिवार की कहानी"]),
        (("किसान", "गाँव", "गांव", "village", "farmer"),
         ["village story", "ग्रामीण कहानी"]),
        (("भगवान", "भक्ति", "मंदिर", "devotional", "कृष्ण", "राम"),
         ["devotional story", "भक्ति कहानी"]),
    ]

    for keywords, additions in groups:
        if any(word in text for word in keywords):
            tags.extend(additions)

    unique = []
    seen = set()
    for tag in tags:
        tag = clean(tag, 100).strip(" ,#")
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            unique.append(tag)
        if len(unique) >= MAX_TAGS:
            break
    return unique


def local_fallback(title, context, language):
    """Create relevant metadata without calling a paid or external model."""
    description = (
        f"{title} — एक कहानी जिसमें घटनाएँ, पात्र और उनके फैसले "
        "कहानी के घटनाक्रम के साथ सामने आते हैं। पूरी कहानी देखें।"
    )
    return {
        "title": clean(title, MAX_TITLE_LENGTH),
        "description": clean(description, MAX_DESCRIPTION_LENGTH),
        "tags": infer_tags(title, context),
        "language": clean(language, 40) or "Hindi",
    }


def normalize_metadata(data, fallback_title, context, language):
    if not isinstance(data, dict):
        raise ValueError("Metadata response must be a JSON object.")

    title = clean(data.get("title"), MAX_TITLE_LENGTH) or fallback_title
    description = clean(data.get("description"), MAX_DESCRIPTION_LENGTH)

    raw_tags = data.get("tags", [])
    if not isinstance(raw_tags, list):
        raw_tags = []

    tags = []
    seen = set()
    for item in raw_tags:
        tag = clean(item, 100).strip(" ,#")
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break

    # Replace generic/empty tags with story-context-derived tags.
    if len(tags) < 4:
        tags = infer_tags(title, context)

    if not description or description.casefold() in {
        title.casefold(), "story", "hindi story"
    }:
        description = (
            f"{title} — पात्रों, घटनाओं और फैसलों से जुड़ी "
            "एक सिनेमैटिक हिंदी कहानी।"
        )

    return {
        "title": title,
        "description": clean(description, MAX_DESCRIPTION_LENGTH),
        "tags": tags[:MAX_TAGS],
        "language": clean(data.get("language") or language, 40) or "Hindi",
    }


def request_gemini(api_key, model, title, context, language):
    schema = {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "description": {"type": "STRING"},
            "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["title", "description", "tags"],
    }

    prompt = f"""
Create accurate YouTube metadata for a Hindi cinematic story.

STORY TITLE / TOPIC:
{title}

STORY CONTEXT:
{context[:14000]}

LANGUAGE:
{language or "Hindi"}

Rules:
- Return a truthful, engaging title of at most 100 characters.
- If the supplied title is already meaningful, improve it only when useful.
- Do not use the first narration sentence as a title by default.
- Write a specific description in 2-3 short sentences, maximum 1000 characters.
- Do not reveal the ending or invent events.
- Provide 8-15 relevant tags, with no unrelated or generic keyword stuffing.
- Return JSON only, matching the schema.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0.3,
        },
    }

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
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
            log(f"Metadata API attempt {attempt}/{MAX_RETRIES}")
            with urllib.request.urlopen(request, timeout=75) as response:
                result = json.loads(response.read().decode("utf-8"))

            candidates = result.get("candidates", [])
            parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
            response_text = next(
                (p.get("text", "") for p in parts
                 if isinstance(p, dict) and p.get("text")),
                "",
            )
            if not response_text:
                raise ValueError("Model returned no metadata text.")

            parsed = json.loads(response_text)
            return normalize_metadata(parsed, title, context, language)

        except urllib.error.HTTPError as exc:
            last_error = exc
            log(f"Metadata API HTTP error: {exc.code}")
            if exc.code in (400, 401, 403, 404):
                break
        except (
            urllib.error.URLError, TimeoutError, OSError,
            ValueError, KeyError, IndexError, json.JSONDecodeError,
        ) as exc:
            last_error = exc
            log(f"Metadata API attempt failed: {exc}")

        if attempt < MAX_RETRIES:
            time.sleep(attempt * 2)

    raise RuntimeError(f"Metadata generation failed: {last_error}")


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

    def atomic_text(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    temporary_json = OUTPUT_JSON.with_suffix(".json.tmp")
    temporary_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_json.replace(OUTPUT_JSON)

    atomic_text(OUTPUT_TITLE, metadata["title"] + "\n")
    atomic_text(OUTPUT_DESCRIPTION, metadata["description"] + "\n")
    atomic_text(OUTPUT_TAGS, ", ".join(metadata["tags"]) + "\n")

    package_text = (
        "TITLE\n"
        f"{metadata['title']}\n\n"
        "DESCRIPTION\n"
        f"{metadata['description']}\n\n"
        "TAGS\n"
        f"{', '.join(metadata['tags'])}\n\n"
        f"LANGUAGE\n{metadata['language']}\n"
    )
    atomic_text(PACKAGE_METADATA, package_text)

    atomic_text(
        CHECKPOINT,
        json.dumps({
            "status": "completed",
            "version": VERSION,
            "input_fingerprint": input_fingerprint,
            "source": source,
        }, ensure_ascii=False, indent=2) + "\n",
    )


def load_cache(expected_fingerprint, title, context, language):
    data = read_json(OUTPUT_JSON)
    if not isinstance(data, dict):
        return None

    if (
        data.get("status") != "completed"
        or data.get("version") != VERSION
        or data.get("input_fingerprint") != expected_fingerprint
    ):
        return None

    try:
        return normalize_metadata(data, title, context, language)
    except (ValueError, TypeError):
        return None


def main():
    values = read_input()
    story = read_json(INPUT_STORY)
    model = load_model()

    raw_topic = clean(values.get("TOPIC") or values.get("TITLE"))
    title = derive_title(raw_topic, story)

    context = "\n".join(story_strings(story))[:18000]
    if not context:
        context = title

    language = (
        values.get("LANGUAGE")
        or values.get("CAPTIONS")
        or "Hindi"
    )

    fingerprint_value = fingerprint(values, story, model, title)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cached = load_cache(fingerprint_value, title, context, language)
    if cached:
        log("Matching metadata cache found; skipping API request.")
        save_outputs(cached, fingerprint_value, "cache")
        log(f"Title: {cached['title']}")
        return

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key and model:
        try:
            metadata = request_gemini(
                api_key, model, title, context, language
            )
            source = "gemini"
        except RuntimeError as exc:
            log(f"WARNING: {exc}")
            log("Using local metadata fallback; no further API requests.")
            metadata = local_fallback(title, context, language)
            source = "local_fallback"
    else:
        log("Gemini key/model unavailable; using local metadata fallback.")
        metadata = local_fallback(title, context, language)
        source = "local_fallback"

    save_outputs(metadata, fingerprint_value, source)

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