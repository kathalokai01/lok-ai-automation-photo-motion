#!/usr/bin/env python3
"""Story-specific YouTube metadata with local fallback and resumable cache."""

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

VERSION = "metadata-v5-story-specific-description"
MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 1000
MAX_TAGS = 20
MAX_RETRIES = 2


def log(*values):
    print(*values, flush=True)


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
        log("WARNING: Cannot read", path.name, "-", exc)
        return {}

    return data if isinstance(data, (dict, list)) else {}


def read_input():
    if not INPUT_TOPIC.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    values = {}
    lines = INPUT_TOPIC.read_text(encoding="utf-8-sig").splitlines()

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip().upper()] = value.strip().strip("\"'")

    return values


def story_strings(value, depth=0):
    """Collect meaningful story text without serializing arbitrary metadata."""
    if depth > 7:
        return []

    if isinstance(value, str):
        value = clean(value)
        return [value] if value else []

    if isinstance(value, list):
        result = []
        for item in value[:100]:
            result.extend(story_strings(item, depth + 1))
        return result

    if not isinstance(value, dict):
        return []

    result = []
    preferred = (
        "title", "story_title", "generated_title", "topic",
        "logline", "summary", "description", "synopsis",
        "story", "full_story", "story_text", "narration",
        "text", "content", "parts", "scenes",
    )

    for key in preferred:
        if key in value:
            result.extend(story_strings(value[key], depth + 1))

    return result


def story_text_candidates(story):
    """Return actual narrative/summary candidates in preference order."""
    if not isinstance(story, dict):
        return []

    candidates = []

    for key in (
        "summary", "logline", "synopsis", "description",
        "story", "full_story", "story_text", "narration", "content",
    ):
        value = story.get(key)
        if isinstance(value, str) and clean(value):
            candidates.append(clean(value))

    for container_key in ("metadata", "story_metadata"):
        container = story.get(container_key)
        if isinstance(container, dict):
            for key in ("summary", "logline", "synopsis", "description"):
                value = container.get(key)
                if isinstance(value, str) and clean(value):
                    candidates.append(clean(value))

    parts = story.get("parts")
    if isinstance(parts, list):
        part_texts = []
        for part in parts:
            if isinstance(part, str):
                part_texts.append(clean(part))
            elif isinstance(part, dict):
                for key in ("summary", "story", "text", "narration", "content"):
                    value = part.get(key)
                    if isinstance(value, str) and clean(value):
                        part_texts.append(clean(value))
                        break

        if part_texts:
            candidates.append(" ".join(part_texts))

    # Prefer longer meaningful text, while preserving first-choice ordering
    # when candidates have similar lengths.
    unique = []
    seen = set()

    for candidate in candidates:
        candidate = clean(candidate)
        key = candidate.casefold()
        if len(candidate) >= 20 and key not in seen:
            seen.add(key)
            unique.append(candidate)

    return sorted(unique, key=len, reverse=True)


def read_story_title(story):
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
                for key in ("title", "story_title", "generated_title"):
                    candidate = clean(container.get(key), MAX_TITLE_LENGTH)
                    if candidate:
                        return candidate

    return ""


def derive_title(topic, story):
    topic = clean(topic, MAX_TITLE_LENGTH)
    if topic:
        return topic

    title = read_story_title(story)
    if title:
        return title

    candidates = story_text_candidates(story)
    if candidates:
        candidate = candidates[0]
        first_sentence = re.split(r"(?<=[.!?।])\s+", candidate, maxsplit=1)[0]
        first_sentence = clean(first_sentence, 65).rstrip(" ,;:-।")
        if len(first_sentence) >= 8:
            return clean(f"कहानी: {first_sentence}", MAX_TITLE_LENGTH)

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


def split_sentences(text):
    text = clean(text)
    if not text:
        return []

    return [
        clean(sentence)
        for sentence in re.split(r"(?<=[.!?।])\s+", text)
        if clean(sentence)
    ]


def build_story_description(title, story, language):
    """Create a story-specific local description without inventing plot facts."""
    candidates = story_text_candidates(story)
    chosen = candidates[0] if candidates else ""

    if chosen:
        sentences = split_sentences(chosen)
        selected = []
        length = 0

        for sentence in sentences:
            # Avoid leading metadata labels and repeated title-only fragments.
            sentence = re.sub(
                r"^(summary|story|narration|description)\s*:\s*",
                "",
                sentence,
                flags=re.IGNORECASE,
            ).strip()

            if not sentence or sentence.casefold() == title.casefold():
                continue

            if length + len(sentence) > 620:
                remaining = 620 - length
                if remaining >= 80:
                    selected.append(sentence[:remaining].rsplit(" ", 1)[0].rstrip(" ,;:-"))
                break

            selected.append(sentence)
            length += len(sentence) + 1

            if len(selected) >= 3:
                break

        story_excerpt = clean(" ".join(selected), 650)
    else:
        story_excerpt = ""

    if story_excerpt:
        description = (
            f"{title}\n\n"
            f"{story_excerpt}\n\n"
            "कहानी के पात्रों, घटनाओं और आगे आने वाले मोड़ों को जानने के लिए "
            "वीडियो अंत तक देखें।"
        )
    else:
        description = (
            f"{title}\n\n"
            f"यह {language or 'Hindi'} कहानी अपने शीर्षक और उपलब्ध कथानक "
            "के आधार पर प्रस्तुत की गई है। पूरी कहानी देखने के लिए वीडियो "
            "अंत तक देखें।"
        )

    return clean(description, MAX_DESCRIPTION_LENGTH)


def infer_tags(title, context):
    text = f"{title} {context}".casefold()
    tags = [title, "Hindi story", "Hindi kahani", "हिंदी कहानी"]

    groups = [
        (("रहस्य", "mystery", "secret", "गुमशुदा", "गायब"),
         ["mystery story", "रहस्य कहानी"]),
        (("भूत", "डरावनी", "हॉरर", "horror", "ghost", "haunted", "चुड़ैल"),
         ["horror story", "डरावनी कहानी"]),
        (("प्रेम", "प्यार", "love", "romance", "प्रेम कहानी"),
         ["love story", "प्रेम कहानी"]),
        (("अपराध", "जुर्म", "crime", "criminal", "पुलिस"),
         ["crime story", "अपराध कहानी"]),
        (("प्रेरणा", "प्रेरणादायक", "motivational", "inspiration"),
         ["inspirational story", "प्रेरणादायक कहानी"]),
        (("सस्पेंस", "रोमांच", "thriller", "suspense"),
         ["suspense story", "thriller kahani"]),
        (("इतिहास", "ऐतिहासिक", "historical", "king", "राजा", "रानी"),
         ["historical story", "ऐतिहासिक कहानी"]),
        (("हास्य", "मजेदार", "comedy", "funny"),
         ["comedy story", "हास्य कहानी"]),
        (("माँ", "मां", "पिता", "परिवार", "बेटा", "बेटी",
          "mother", "father", "family"),
         ["family story", "परिवार की कहानी"]),
        (("किसान", "गाँव", "गांव", "village", "farmer"),
         ["village story", "ग्रामीण कहानी"]),
        (("भगवान", "भक्ति", "मंदिर", "devotional", "कृष्ण", "राम", "शिव"),
         ["devotional story", "भक्ति कहानी"]),
        (("जंगल", "वन", "पहाड़", "नदी", "प्रकृति", "forest", "nature"),
         ["nature story", "प्रकृति की कहानी"]),
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


def local_fallback(title, context, language, story):
    return {
        "title": clean(title, MAX_TITLE_LENGTH),
        "description": build_story_description(title, story, language),
        "tags": infer_tags(title, context),
        "language": clean(language, 40) or "Hindi",
    }


def normalize_metadata(data, fallback_title, context, language, story):
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

    if len(tags) < 4:
        tags = infer_tags(title, context)

    generic_descriptions = {
        title.casefold(),
        "story",
        "hindi story",
        "एक हिंदी कहानी",
    }

    if not description or description.casefold() in generic_descriptions:
        description = build_story_description(title, story, language)

    return {
        "title": title,
        "description": clean(description, MAX_DESCRIPTION_LENGTH),
        "tags": tags[:MAX_TAGS],
        "language": clean(data.get("language") or language, 40) or "Hindi",
    }


def request_gemini(api_key, model, title, context, language, story):
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
Create accurate YouTube metadata for this Hindi story.

TITLE OR TOPIC:
{title}

STORY CONTEXT:
{context[:14000]}

LANGUAGE:
{language or "Hindi"}

Rules:
- Keep the title under 100 characters.
- Do not use the first narration sentence as the title by default.
- The description must refer to actual story events or characters present
  in the supplied context. Do not invent characters, events, or an ending.
- Write 2-3 concise, engaging sentences, maximum 1000 characters.
- Do not reveal the ending.
- Give 8-15 relevant tags, with no unrelated keyword stuffing.
- Return JSON only.
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
            parts = (
                candidates[0].get("content", {}).get("parts", [])
                if candidates else []
            )
            response_text = next(
                (
                    item.get("text", "")
                    for item in parts
                    if isinstance(item, dict) and item.get("text")
                ),
                "",
            )

            if not response_text:
                raise ValueError("Model returned no metadata text.")

            parsed = json.loads(response_text)
            return normalize_metadata(
                parsed, title, context, language, story
            )

        except urllib.error.HTTPError as exc:
            last_error = exc
            log("Metadata API HTTP error:", exc.code)

            if exc.code in (400, 401, 403, 404):
                break

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            KeyError,
            IndexError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            log("Metadata API attempt failed:", exc)

        if attempt < MAX_RETRIES:
            time.sleep(attempt * 2)

    raise RuntimeError(f"Metadata generation failed: {last_error}")


def atomic_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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

    temporary_json = OUTPUT_JSON.with_suffix(".json.tmp")

    try:
        temporary_json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_json.replace(OUTPUT_JSON)
    finally:
        temporary_json.unlink(missing_ok=True)

    atomic_text(OUTPUT_TITLE, metadata["title"] + "\n")
    atomic_text(OUTPUT_DESCRIPTION, metadata["description"] + "\n")
    atomic_text(OUTPUT_TAGS, ", ".join(metadata["tags"]) + "\n")

    # Keep the headings compatible with validate_final_output.py.
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
        json.dumps(
            {
                "status": "completed",
                "version": VERSION,
                "input_fingerprint": input_fingerprint,
                "source": source,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
    )


def load_cache(expected_fingerprint, title, context, language, story):
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
        return normalize_metadata(
            data, title, context, language, story
        )
    except (ValueError, TypeError):
        return None


def main():
    values = read_input()
    story = read_json(INPUT_STORY)
    model = load_model()

    raw_topic = clean(values.get("TOPIC") or values.get("TITLE"))
    title = derive_title(raw_topic, story)

    candidates = story_text_candidates(story)
    context_parts = story_strings(story)
    context = "\n".join(context_parts)[:18000]

    if not context and candidates:
        context = candidates[0]

    if not context:
        context = title

    language = (
        values.get("LANGUAGE")
        or values.get("CAPTIONS")
        or "Hindi"
    )

    fingerprint_value = fingerprint(values, story, model, title)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cached = load_cache(
        fingerprint_value, title, context, language, story
    )

    if cached:
        log("Matching metadata cache found; skipping API request.")
        save_outputs(cached, fingerprint_value, "cache")
        log("Title:", cached["title"])
        log("Description length:", len(cached["description"]))
        return

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if api_key and model:
        try:
            metadata = request_gemini(
                api_key, model, title, context, language, story
            )
            source = "gemini"
        except RuntimeError as exc:
            log("WARNING:", exc)
            log("Using local story-specific fallback; no more API requests.")
            metadata = local_fallback(title, context, language, story)
            source = "local_fallback"
    else:
        log("Gemini key/model unavailable; using local story-specific fallback.")
        metadata = local_fallback(title, context, language, story)
        source = "local_fallback"

    save_outputs(metadata, fingerprint_value, source)

    log("======================================")
    log("YOUTUBE METADATA READY")
    log("Source:", source)
    log("Title:", metadata["title"])
    log("Description length:", len(metadata["description"]))
    log("Tags:", len(metadata["tags"]))
    log("Package file:", PACKAGE_METADATA.relative_to(ROOT))
    log("======================================")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("ERROR:", exc)
        raise SystemExit(1)