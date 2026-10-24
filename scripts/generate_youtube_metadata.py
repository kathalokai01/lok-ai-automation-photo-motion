
#!/usr/bin/env python3
"""Story-aware YouTube metadata with nested narration extraction and cache."""

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

VERSION = "metadata-v6-nested-story-context"
MAX_TITLE = 100
MAX_DESCRIPTION = 1000
MAX_TAGS = 20
MAX_CONTEXT = 14000


def log(*args):
    print(*args, flush=True)


def clean(value, limit=None):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit].rstrip() if limit else value


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, (dict, list)) else {}
    except (OSError, ValueError):
        return {}


def write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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


def strings_from(value, depth=0):
    """Collect narrative text, including parts[].scenes[].narration."""
    if depth > 10:
        return []

    if isinstance(value, str):
        value = clean(value)
        return [value] if value else []

    if isinstance(value, list):
        result = []
        for item in value[:500]:
            result.extend(strings_from(item, depth + 1))
        return result

    if not isinstance(value, dict):
        return []

    result = []

    # Narrative fields first; skip prompts and technical metadata.
    for key in (
        "summary", "logline", "synopsis", "story_text", "full_story",
        "story", "narration", "text", "content", "description",
    ):
        item = value.get(key)
        if isinstance(item, str) and clean(item):
            result.append(clean(item))

    # Handle nested story/AI-story schemas explicitly.
    for key in ("parts", "scenes", "story_parts", "chapters"):
        item = value.get(key)
        if isinstance(item, (dict, list)):
            result.extend(strings_from(item, depth + 1))

    # Include other narrative containers, but not arbitrary prompts.
    for key in ("metadata", "story_metadata", "plot", "ending"):
        item = value.get(key)
        if isinstance(item, (dict, list)):
            result.extend(strings_from(item, depth + 1))
        elif isinstance(item, str) and clean(item):
            result.append(clean(item))

    return result


def read_story():
    for path in (STORY_FILE, SOURCE_STORY_FILE):
        data = read_json(path)
        if isinstance(data, dict) and data.get("status") == "completed":
            return data
    return {}


def read_title_file():
    if not TITLE_FILE.is_file():
        return ""
    for line in TITLE_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = clean(line, MAX_TITLE)
        if line and not line.startswith(("#", "[")):
            return line
    return ""


def title_from_story(story, topic, context):
    if topic:
        return clean(topic, MAX_TITLE)

    saved_title = read_title_file()
    if saved_title:
        return saved_title

    for container in (
        story,
        story.get("metadata", {}) if isinstance(story, dict) else {},
        story.get("story_metadata", {}) if isinstance(story, dict) else {},
    ):
        if not isinstance(container, dict):
            continue
        for key in ("title", "story_title", "generated_title"):
            value = clean(container.get(key), MAX_TITLE)
            if value:
                return value

    # Do not use the first narration sentence as the title.
    # Derive a short label from the story's recurring subject words.
    text = clean(context)
    words = re.findall(r"[\w\u0900-\u097F]+", text)
    ignored = {
        "एक", "और", "था", "थी", "थे", "उस", "उसके", "उसकी", "लेकिन",
        "फिर", "तभी", "कहानी", "उसने", "वह", "यह", "जो", "की", "के",
        "का", "में", "से", "पर", "को", "है", "the", "and", "was",
        "were", "then", "when", "with", "from", "story", "that",
    }
    useful = []
    seen = set()
    for word in words:
        normalized = word.casefold()
        if len(word) < 3 or normalized in ignored or normalized in seen:
            continue
        seen.add(normalized)
        useful.append(word)
        if len(useful) >= 5:
            break

    if useful:
        return clean(" ".join(useful) + " | हिंदी कहानी", MAX_TITLE)

    return "हिंदी कहानी"


def story_context(story):
    candidates = strings_from(story)
    unique = []
    seen = set()

    for value in candidates:
        value = clean(value)
        key = value.casefold()
        if len(value) < 20 or key in seen:
            continue
        seen.add(key)
        unique.append(value)

    # Long narrative passages are more useful than isolated labels.
    unique.sort(key=len, reverse=True)
    return clean(" ".join(unique), MAX_CONTEXT)


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

    unique = []
    seen = set()
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
        clean(part)
        for part in re.split(r"(?<=[.!?।])\s+", context)
        if clean(part)
    ]

    chosen = []
    total = 0
    for sentence in sentences:
        if sentence.casefold() == title.casefold():
            continue
        if len(sentence) > 500:
            sentence = sentence[:500].rsplit(" ", 1)[0]
        if total + len(sentence) > 650:
            remaining = 650 - total
            if remaining >= 80:
                chosen.append(sentence[:remaining].rsplit(" ", 1)[0])
            break
        chosen.append(sentence)
        total += len(sentence) + 1
        if len(chosen) >= 3:
            break

    excerpt = clean(" ".join(chosen), 650)
    if excerpt:
        description = f"{title}\n\n{excerpt}"
        description += (
            "\n\nकहानी के पात्रों और घटनाओं के अगले मोड़ जानने के लिए "
            "वीडियो अंत तक देखें।"
        )
    else:
        description = (
            f"{title}\n\nयह {language} कहानी प्रस्तुत है। "
            "वीडियो में उपलब्ध कथानक और घटनाओं को देखें।"
        )

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


def load_model():
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict):
        return ""
    if data.get("status") not in (None, "", "selected"):
        return ""
    return clean(data.get("model"), 150)


def normalize_metadata(data, fallback, context, language, mode):
    if not isinstance(data, dict):
        raise ValueError("Metadata response must be a JSON object.")

    title = clean(data.get("title"), MAX_TITLE) or fallback
    description = clean(data.get("description"), MAX_DESCRIPTION)

    raw_tags = data.get("tags", [])
    if not isinstance(raw_tags, list):
        raw_tags = []

    tags = []
    seen = set()
    for value in raw_tags:
        tag = clean(value, 100).strip(" ,#")
        if tag and tag.casefold() not in seen:
            tags.append(tag)
            seen.add(tag.casefold())
        if len(tags) >= MAX_TAGS:
            break

    local = make_local_metadata(title, context, language, mode)
    if not description or len(description) < 40:
        description = local["description"]
    if len(tags) < 4:
        tags = local["tags"]

    return {
        "title": title,
        "description": clean(description, MAX_DESCRIPTION),
        "tags": tags[:MAX_TAGS],
        "language": clean(data.get("language") or language, 40) or "Hindi",
    }


def request_gemini(api_key, model, title, context, language, mode):
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
Create YouTube metadata for this actual story.
Title/topic: {title}
Story context: {context[:MAX_CONTEXT]}
Language: {language}
Format: {mode}

Rules:
- Title under 100 characters; do not use the first narration sentence
  as the title by default.
- Use only characters and events supported by the supplied story.
- Description should summarize the story without revealing its ending.
- Provide 8-15 relevant tags; avoid unrelated keyword stuffing.
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

    for attempt in range(1, 3):
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
            log(f"Metadata Gemini request {attempt}/2")
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))

            candidates = result.get("candidates", [])
            parts = (
                candidates[0].get("content", {}).get("parts", [])
                if candidates else []
            )
            text = next(
                (p.get("text", "") for p in parts
                 if isinstance(p, dict) and p.get("text")),
                "",
            )
            if not text:
                raise ValueError("Gemini returned empty metadata.")
            return normalize_metadata(
                json.loads(text), title, context, language, mode
            )

        except urllib.error.HTTPError as exc:
            last_error = exc
            log("Metadata API HTTP error:", exc.code)
            if exc.code in (400, 401, 403, 404):
                break
        except (
            urllib.error.URLError, TimeoutError, OSError,
            ValueError, KeyError, IndexError, json.JSONDecodeError,
        ) as exc:
            last_error = exc
            log("Metadata request failed:", exc)

        if attempt < 2:
            time.sleep(attempt)

    raise RuntimeError(f"Gemini metadata failed: {last_error}")


def save_outputs(metadata, fingerprint, source):
    PUBLISH.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "completed",
        "version": VERSION,
        "source": source,
        "input_fingerprint": fingerprint,
        **metadata,
    }

    temporary = OUTPUT_JSON.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(OUTPUT_JSON)
    finally:
        temporary.unlink(missing_ok=True)

    write_text(OUTPUT_TITLE, metadata["title"] + "\n")
    write_text(OUTPUT_DESCRIPTION, metadata["description"] + "\n")
    write_text(OUTPUT_TAGS, ", ".join(metadata["tags"]) + "\n")

    # Preserve the heading format accepted by final-output validation.
    package_text = (
        "TITLE\n"
        f"{metadata['title']}\n\n"
        "DESCRIPTION\n"
        f"{metadata['description']}\n\n"
        "TAGS\n"
        f"{', '.join(metadata['tags'])}\n\n"
        f"LANGUAGE\n{metadata['language']}\n"
    )
    write_text(PACKAGE_METADATA, package_text)
    write_text(CHECKPOINT, json.dumps({
        "status": "completed",
        "version": VERSION,
        "input_fingerprint": fingerprint,
        "source": source,
    }, ensure_ascii=False, indent=2) + "\n")


def main():
    values = read_input()
    story = read_story()

    topic = clean(values.get("TOPIC") or values.get("TITLE"))
    context = story_context(story)

    if not context:
        # If the AI story file is missing/incomplete, retain source-story text.
        context = story_context(read_json(SOURCE_STORY_FILE))

    if not context:
        context = clean(values.get("STORY_TEXT") or values.get("STORY") or "")

    title = title_from_story(story, topic, context)
    language = clean(values.get("LANGUAGE") or values.get("CAPTIONS") or "Hindi", 40)
    mode = clean(values.get("FORMAT") or "full").lower()
    mode = "short" if mode in {"short", "shorts", "vertical", "reels"} else "full"

    model = load_model()
    fingerprint_payload = {
        "version": VERSION,
        "input": values,
        "story": story,
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
            log("Using local story-based metadata.")
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
