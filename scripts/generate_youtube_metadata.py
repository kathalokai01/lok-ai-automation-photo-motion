#!/usr/bin/env python3
"""Story-specific YouTube metadata with cache and one-request API policy."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "ai_story.json"
SOURCE_STORY_FILE = ROOT / "output" / "story" / "story.json"
TITLE_FILE = ROOT / "output" / "story" / "final_title.txt"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

OUT = ROOT / "output"
PUBLISH = OUT / "publish"
OUTPUT_JSON = PUBLISH / "youtube_metadata.json"
OUTPUT_TITLE = PUBLISH / "youtube_title.txt"
OUTPUT_DESCRIPTION = PUBLISH / "youtube_description.txt"
OUTPUT_TAGS = PUBLISH / "youtube_tags.txt"
CHECKPOINT = PUBLISH / "metadata_checkpoint.json"
PACKAGE_METADATA = OUT / "youtube_metadata.txt"

VERSION = "metadata-v7-story-specific-single-request"
MAX_TITLE = 100
MAX_DESCRIPTION = 1000
MAX_TAGS = 20
MAX_CONTEXT = 12000


def log(*args):
    print(*args, flush=True)


def clean(value, limit=None):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit].rstrip() if limit else value


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, (dict, list)) else {}
    except (OSError, ValueError):
        return {}


def write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_input():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    result = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip().upper()] = value.strip().strip("\"'")
    return result


def story_strings(value, depth=0):
    """Read story content only; ignore technical prompts and image instructions."""
    if depth > 10:
        return []

    if isinstance(value, str):
        text = clean(value)
        return [text] if len(text) >= 20 else []

    if isinstance(value, list):
        result = []
        for item in value[:400]:
            result.extend(story_strings(item, depth + 1))
        return result

    if not isinstance(value, dict):
        return []

    result = []
    narrative_fields = (
        "summary", "logline", "synopsis", "story_text", "full_story",
        "story", "narration", "text", "content",
    )
    container_fields = (
        "parts", "scenes", "story_parts", "chapters",
        "plot", "ending", "story_metadata", "metadata",
    )

    for key in narrative_fields:
        item = value.get(key)
        if isinstance(item, str):
            text = clean(item)
            if len(text) >= 20:
                result.append(text)

    for key in container_fields:
        item = value.get(key)
        if isinstance(item, (dict, list)):
            result.extend(story_strings(item, depth + 1))

    return result


def story_context(story):
    values = story_strings(story)
    result = []
    seen = set()

    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)

    # Keep source order so the opening, plot and ending remain in order.
    return clean(" ".join(result), MAX_CONTEXT)


def read_title_file():
    if not TITLE_FILE.is_file():
        return ""
    for line in TITLE_FILE.read_text(encoding="utf-8-sig").splitlines():
        value = clean(line, MAX_TITLE)
        if value and not value.startswith(("#", "[")):
            return value
    return ""


def title_from_story(story, topic, context):
    # An explicitly supplied topic/title takes priority.
    if topic:
        return clean(topic, MAX_TITLE)

    # The story-generation stage may already have created a title.
    saved = read_title_file()
    if saved:
        return saved

    if isinstance(story, dict):
        containers = [
            story,
            story.get("metadata", {}),
            story.get("story_metadata", {}),
        ]
        for container in containers:
            if not isinstance(container, dict):
                continue
            for key in ("title", "story_title", "generated_title"):
                value = clean(container.get(key), MAX_TITLE)
                if value:
                    return value

    # Last-resort title derived from story keywords, not its first sentence.
    ignored = {
        "एक", "और", "था", "थी", "थे", "उस", "उसके", "उसकी",
        "लेकिन", "फिर", "तभी", "कहानी", "उसने", "वह", "यह",
        "जो", "की", "के", "का", "में", "से", "पर", "को", "है",
        "the", "and", "was", "were", "then", "when", "with",
        "from", "story", "that", "this", "have", "they",
    }
    words = re.findall(r"[\w\u0900-\u097F]+", context)
    chosen, seen = [], set()

    for word in words:
        key = word.casefold()
        if len(word) < 3 or key in ignored or key in seen:
            continue
        chosen.append(word)
        seen.add(key)
        if len(chosen) == 5:
            break

    if chosen:
        return clean(" ".join(chosen) + " | हिंदी कहानी", MAX_TITLE)
    return "हिंदी कहानी"


def infer_tags(title, context, mode):
    combined = f"{title} {context}".casefold()
    tags = [title, "Hindi story", "Hindi kahani", "हिंदी कहानी"]

    categories = [
        (("भूत", "प्रेत", "हवेली", "डरावनी", "horror", "ghost", "haunted"),
         ["horror story", "डरावनी कहानी"]),
        (("रहस्य", "गायब", "गुमशुदा", "पहेली", "mystery", "secret"),
         ["mystery story", "रहस्य कहानी"]),
        (("सस्पेंस", "रोमांच", "thriller", "suspense"),
         ["suspense story", "thriller story"]),
        (("प्रेम", "प्यार", "रोमांस", "love", "romance"),
         ["love story", "प्रेम कहानी"]),
        (("अपराध", "पुलिस", "चोर", "जुर्म", "crime", "criminal"),
         ["crime story", "अपराध कहानी"]),
        (("माँ", "मां", "पिता", "परिवार", "बेटा", "बेटी",
          "mother", "father", "family"),
         ["family story", "परिवार की कहानी"]),
        (("किसान", "गाँव", "गांव", "जंगल", "नदी", "farmer", "village"),
         ["village story", "ग्रामीण कहानी"]),
        (("भगवान", "भक्ति", "मंदिर", "कृष्ण", "राम", "शिव", "devotional"),
         ["devotional story", "भक्ति कहानी"]),
        (("राजा", "रानी", "सम्राट", "किला", "युद्ध", "king", "queen"),
         ["historical story", "ऐतिहासिक कहानी"]),
        (("प्रेरणा", "प्रेरणादायक", "motivational", "inspiration"),
         ["motivational story", "प्रेरणादायक कहानी"]),
    ]

    for keywords, additions in categories:
        if any(word.casefold() in combined for word in keywords):
            tags.extend(additions)

    if mode == "short":
        tags.extend(["Hindi shorts", "story shorts"])

    unique, seen = [], set()
    for item in tags:
        item = clean(item, 100).strip(" ,#")
        key = item.casefold()
        if item and key not in seen:
            unique.append(item)
            seen.add(key)
        if len(unique) >= MAX_TAGS:
            break
    return unique


def make_local_metadata(title, context, language, mode):
    sentences = [
        clean(s) for s in re.split(r"(?<=[.!?।])\s+", context)
        if clean(s)
    ]

    excerpt_parts, total = [], 0
    for sentence in sentences:
        if sentence.casefold() == title.casefold():
            continue
        if len(sentence) > 400:
            sentence = sentence[:400].rsplit(" ", 1)[0]
        if total + len(sentence) > 600:
            remaining = 600 - total
            if remaining >= 60:
                excerpt_parts.append(sentence[:remaining].rsplit(" ", 1)[0])
            break
        excerpt_parts.append(sentence)
        total += len(sentence) + 1
        if len(excerpt_parts) >= 3:
            break

    excerpt = clean(" ".join(excerpt_parts), 600)
    if excerpt:
        description = f"{title}\n\n{excerpt}"
        description += (
            "\n\nकहानी के अगले मोड़ जानने के लिए वीडियो अंत तक देखें।"
        )
    else:
        description = f"{title}\n\nयह हिंदी कहानी प्रस्तुत है।"

    if mode == "short":
        description += "\n\n#HindiShorts #HindiKahani"
    else:
        description += "\n\n#HindiKahani #HindiStory"

    return {
        "title": clean(title, MAX_TITLE),
        "description": clean(description, MAX_DESCRIPTION),
        "tags": infer_tags(title, context, mode),
        "language": clean(language, 40) or "Hindi",
    }


def selected_model():
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict):
        return ""

    # Accept common field names; do not reject a valid model just because
    # another script uses a different status label.
    for key in ("model", "selected_model", "model_name", "name"):
        value = clean(data.get(key), 150)
        if value:
            return value
    return ""


def normalize_metadata(data, fallback, context, language, mode):
    if not isinstance(data, dict):
        raise ValueError("Metadata response was not a JSON object.")

    title = clean(data.get("title"), MAX_TITLE) or fallback
    local = make_local_metadata(title, context, language, mode)

    description = clean(data.get("description"), MAX_DESCRIPTION)
    if len(description) < 40:
        description = local["description"]

    raw_tags = data.get("tags", [])
    if not isinstance(raw_tags, list):
        raw_tags = []

    tags, seen = [], set()
    for value in raw_tags:
        tag = clean(value, 100).strip(" ,#")
        if tag and tag.casefold() not in seen:
            tags.append(tag)
            seen.add(tag.casefold())
        if len(tags) >= MAX_TAGS:
            break

    # Prevent empty or generic API tags from replacing useful story tags.
    if len(tags) < 4:
        tags = local["tags"]

    return {
        "title": title,
        "description": clean(description, MAX_DESCRIPTION),
        "tags": tags[:MAX_TAGS],
        "language": clean(data.get("language") or language, 40) or "Hindi",
    }


def request_gemini(api_key, model, title, context, language, mode):
    model = urllib.parse.quote(model, safe="-_.")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + model + ":generateContent"
    )

    prompt = f"""
Create accurate YouTube metadata for the supplied story.

Current title/topic: {title}
Story: {context[:MAX_CONTEXT]}
Language: {language}
Format: {mode}

Rules:
- Return JSON with title, description, tags and language.
- Keep title under 100 characters and make it specific to this story.
- Do not use the first narration sentence as the title.
- Do not invent characters, events, locations or story outcomes.
- The description should tease the plot without revealing the ending.
- Use 8-15 relevant Hindi/English tags; no unrelated keyword stuffing.
- For short format, make metadata appropriate for a short video.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
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

    # One request only: avoid duplicate paid/API attempts in the same run.
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Gemini metadata HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"Gemini metadata request failed: {exc}") from exc

    candidates = result.get("candidates", [])
    parts = (
        candidates[0].get("content", {}).get("parts", [])
        if candidates else []
    )
    response_text = next(
        (p.get("text", "") for p in parts
         if isinstance(p, dict) and p.get("text")),
        "",
    )
    if not response_text:
        raise RuntimeError("Gemini returned empty metadata.")

    try:
        data = json.loads(response_text)
    except ValueError as exc:
        raise RuntimeError("Gemini metadata was not valid JSON.") from exc

    return normalize_metadata(data, title, context, language, mode)


def save_outputs(metadata, fingerprint, source):
    PUBLISH.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "completed",
        "version": VERSION,
        "source": source,
        "input_fingerprint": fingerprint,
        **metadata,
    }

    write_json(OUTPUT_JSON, result)
    write_text(OUTPUT_TITLE, metadata["title"] + "\n")
    write_text(OUTPUT_DESCRIPTION, metadata["description"] + "\n")
    write_text(OUTPUT_TAGS, ", ".join(metadata["tags"]) + "\n")

    # Keep headings expected by the current validation/package pipeline.
    package_text = (
        "TITLE\n" + metadata["title"] + "\n\n"
        "DESCRIPTION\n" + metadata["description"] + "\n\n"
        "TAGS\n" + ", ".join(metadata["tags"]) + "\n\n"
        "LANGUAGE\n" + metadata["language"] + "\n"
    )
    write_text(PACKAGE_METADATA, package_text)
    write_json(CHECKPOINT, {
        "status": "completed",
        "version": VERSION,
        "input_fingerprint": fingerprint,
        "source": source,
    })


def main():
    values = read_input()
    ai_story = read_json(STORY_FILE)
    source_story = read_json(SOURCE_STORY_FILE)

    # Prefer AI story when available, then source story, then supplied text.
    context = story_context(ai_story)
    story_for_title = ai_story if context else source_story
    if not context:
        context = story_context(source_story)
    if not context:
        context = clean(values.get("STORY_TEXT") or values.get("STORY") or "")

    topic = clean(values.get("TOPIC") or values.get("TITLE"))
    title = title_from_story(story_for_title, topic, context)
    language = clean(values.get("LANGUAGE") or values.get("CAPTIONS") or "Hindi", 40)

    mode_value = clean(values.get("FORMAT") or "full").lower()
    mode = "short" if mode_value in {"short", "shorts", "vertical", "reels"} else "full"

    model = selected_model()
    fingerprint_payload = {
        "version": VERSION,
        "input": values,
        "context": context,
        "title": title,
        "model": model,
        "mode": mode,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    cached = read_json(OUTPUT_JSON)
    if (
        isinstance(cached, dict)
        and cached.get("status") == "completed"
        and cached.get("version") == VERSION
        and cached.get("input_fingerprint") == fingerprint
    ):
        log("Matching metadata cache found; no API request.")
        save_outputs(cached, fingerprint, "cache")
        return

    local = make_local_metadata(title, context, language, mode)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if api_key and model:
        try:
            metadata = request_gemini(
                api_key, model, title, context, language, mode
            )
            source = "gemini"
        except RuntimeError as exc:
            log("WARNING:", exc)
            log("Using local story-specific metadata; no retry.")
            metadata = local
            source = "local_fallback"
    else:
        log("Gemini key/model unavailable; using local metadata.")
        metadata = local
        source = "local_fallback"

    save_outputs(metadata, fingerprint, source)
    log("METADATA GENERATION: SUCCESS")
    log("Source:", source)
    log("Format:", mode)
    log("Title:", metadata["title"])
    log("Tags:", len(metadata["tags"]))
    log("Output:", PACKAGE_METADATA.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("METADATA GENERATION ERROR:", exc)
        raise SystemExit(1)