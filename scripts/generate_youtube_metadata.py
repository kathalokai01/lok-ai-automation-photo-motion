
#!/usr/bin/env python3
"""Story-grounded YouTube metadata with cache, validation and local fallback."""

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
AI_STORY_FILE = ROOT / "output" / "story" / "ai_story.json"
STORY_FILE = ROOT / "output" / "story" / "story.json"
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

VERSION = "metadata-v8-story-grounded-validated"
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
    if limit is not None:
        value = value[:limit].rstrip()
    return value


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, (dict, list)) else {}
    except (OSError, ValueError):
        return {}


def write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
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

    values = {}
    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().upper()] = value.strip().strip("\"'")
    return values


def collect_story_text(value, depth=0):
    """Collect narrative fields while excluding visual and technical prompts."""
    if depth > 12:
        return []

    if isinstance(value, str):
        text = clean(value)
        return [text] if len(text) >= 15 else []

    if isinstance(value, list):
        result = []
        for item in value[:500]:
            result.extend(collect_story_text(item, depth + 1))
        return result

    if not isinstance(value, dict):
        return []

    result = []

    for key in (
        "title", "summary", "logline", "synopsis",
        "story_text", "full_story", "story", "narration",
        "dialogue", "ending",
    ):
        item = value.get(key)
        if isinstance(item, str):
            text = clean(item)
            if len(text) >= 15:
                result.append(text)

    for key in ("parts", "scenes", "chapters", "story_parts"):
        item = value.get(key)
        if isinstance(item, (dict, list)):
            result.extend(collect_story_text(item, depth + 1))

    return result


def story_context(story):
    candidates = collect_story_text(story)
    result = []
    seen = set()

    for value in candidates:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)

    return clean(" ".join(result), MAX_CONTEXT)


def read_title_file():
    try:
        for line in TITLE_FILE.read_text(encoding="utf-8-sig").splitlines():
            title = clean(line, MAX_TITLE)
            if title and not title.startswith(("#", "[")):
                return title
    except OSError:
        pass
    return ""


def derive_title(story, explicit_title, context):
    if explicit_title:
        return clean(explicit_title, MAX_TITLE)

    saved = read_title_file()
    if saved:
        return saved

    if isinstance(story, dict):
        for key in ("title", "story_title", "generated_title"):
            title = clean(story.get(key), MAX_TITLE)
            if title:
                return title

    # Avoid using an entire narration sentence as the video title.
    ignored = {
        "एक", "और", "था", "थी", "थे", "उस", "उसने", "उसकी",
        "उसके", "लेकिन", "फिर", "तभी", "कहानी", "वह", "यह",
        "जो", "की", "के", "का", "में", "से", "पर", "को", "है",
        "the", "and", "was", "were", "then", "when", "with",
        "from", "story", "that", "this", "have", "they",
    }

    tokens = re.findall(r"[\w\u0900-\u097F]+", context)
    selected = []
    seen = set()

    for token in tokens:
        key = token.casefold()
        if len(token) < 3 or key in ignored or key in seen:
            continue
        selected.append(token)
        seen.add(key)
        if len(selected) >= 5:
            break

    if selected:
        return clean(" ".join(selected) + " | हिंदी कहानी", MAX_TITLE)

    return "हिंदी कहानी"


def infer_tags(title, context, mode):
    combined = f"{title} {context}".casefold()
    tags = [title, "Hindi story", "Hindi kahani", "हिंदी कहानी"]

    categories = [
        (
            ("भूत", "प्रेत", "हवेली", "डरावनी", "horror", "ghost", "haunted"),
            ["horror story", "डरावनी कहानी"],
        ),
        (
            ("रहस्य", "गायब", "गुमशुदा", "पहेली", "mystery", "secret"),
            ["mystery story", "रहस्य कहानी"],
        ),
        (
            ("सस्पेंस", "रोमांच", "thriller", "suspense"),
            ["suspense story", "thriller story"],
        ),
        (
            ("प्रेम", "प्यार", "रोमांस", "love", "romance"),
            ["love story", "प्रेम कहानी"],
        ),
        (
            ("अपराध", "पुलिस", "चोर", "जुर्म", "crime", "criminal"),
            ["crime story", "अपराध कहानी"],
        ),
        (
            ("माँ", "मां", "पिता", "परिवार", "बेटा", "बेटी",
             "mother", "father", "family"),
            ["family story", "परिवार की कहानी"],
        ),
        (
            ("किसान", "गाँव", "गांव", "जंगल", "नदी", "farmer", "village"),
            ["village story", "ग्रामीण कहानी"],
        ),
        (
            ("भगवान", "भक्ति", "मंदिर", "कृष्ण", "राम", "शिव", "devotional"),
            ["devotional story", "भक्ति कहानी"],
        ),
        (
            ("राजा", "रानी", "सम्राट", "किला", "युद्ध", "king", "queen"),
            ["historical story", "ऐतिहासिक कहानी"],
        ),
        (
            ("प्रेरणा", "प्रेरणादायक", "motivational", "inspiration"),
            ["motivational story", "प्रेरणादायक कहानी"],
        ),
    ]

    for keywords, additions in categories:
        if any(word.casefold() in combined for word in keywords):
            tags.extend(additions)

    if mode == "short":
        tags.extend(["Hindi Shorts", "Hindi story shorts"])

    result = []
    seen = set()

    for item in tags:
        tag = clean(item, 100).strip(" ,#")
        key = tag.casefold()
        if tag and key not in seen:
            result.append(tag)
            seen.add(key)
        if len(result) >= MAX_TAGS:
            break

    return result


def make_local_metadata(title, context, language, mode):
    pieces = [
        clean(part)
        for part in re.split(r"(?<=[.!?।])\s+", context)
        if clean(part)
    ]

    excerpts = []
    total_chars = 0

    for sentence in pieces:
        if sentence.casefold() == title.casefold():
            continue

        sentence = clean(sentence, 300)
        if total_chars + len(sentence) > 550:
            remaining = 550 - total_chars
            if remaining >= 60:
                excerpts.append(sentence[:remaining].rsplit(" ", 1)[0])
            break

        excerpts.append(sentence)
        total_chars += len(sentence) + 1

        if len(excerpts) >= 3:
            break

    excerpt = clean(" ".join(excerpts), 550)

    if excerpt:
        description = f"{title}\n\n{excerpt}"
    else:
        description = f"{title}\n\nयह हिंदी कहानी प्रस्तुत है।"

    if mode == "short":
        description += (
            "\n\nकहानी का अगला मोड़ जानने के लिए वीडियो देखें।"
            "\n\n#HindiShorts #HindiKahani"
        )
    else:
        description += (
            "\n\nकहानी पूरी देखने के लिए वीडियो अंत तक देखें।"
            "\n\n#HindiKahani #HindiStory"
        )

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

    for key in ("model", "selected_model", "model_name", "name"):
        value = clean(data.get(key), 150)
        if value:
            return value
    return ""


def normalize_metadata(data, fallback, context, language, mode):
    if not isinstance(data, dict):
        raise ValueError("Metadata response must be a JSON object.")

    title = clean(data.get("title"), MAX_TITLE) or fallback
    local = make_local_metadata(title, context, language, mode)

    description = clean(data.get("description"), MAX_DESCRIPTION)
    if len(description) < 40:
        description = local["description"]

    raw_tags = data.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = re.split(r"[,;\n]", raw_tags)
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

    # Preserve useful locally inferred tags when the model returns weak tags.
    if len(tags) < 4:
        tags = local["tags"]

    return {
        "title": clean(title, MAX_TITLE),
        "description": clean(description, MAX_DESCRIPTION),
        "tags": tags[:MAX_TAGS],
        "language": clean(data.get("language") or language, 40) or "Hindi",
    }


def request_gemini(api_key, model, title, context, language, mode):
    encoded_model = urllib.parse.quote(model, safe="-_.")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + encoded_model
        + ":generateContent"
    )

    prompt = f"""
Create YouTube metadata for this exact story.

Current topic/title: {title}
Story text: {context[:MAX_CONTEXT]}
Language: {language}
Video format: {mode}

Return JSON only with these keys:
title, description, tags, language.

Rules:
- Title must be under 100 characters and specific to this story.
- Do not simply copy the opening narration sentence as the title.
- Do not invent characters, locations, events or outcomes.
- Description should describe the premise and build curiosity without
  falsely claiming events or spoiling the ending.
- Return 8 to 15 relevant Hindi/English tags.
- Do not add unrelated trending tags or repeat the same tag.
- Short format metadata must suit a short video.
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

    # Exactly one request: do not repeat a possibly billable failed request.
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(
            f"Gemini metadata HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"Gemini metadata request failed: {exc}") from exc

    candidates = result.get("candidates", [])
    content_parts = (
        candidates[0].get("content", {}).get("parts", [])
        if candidates else []
    )

    response_text = next(
        (
            part.get("text", "")
            for part in content_parts
            if isinstance(part, dict) and part.get("text")
        ),
        "",
    )

    if not response_text:
        raise RuntimeError("Gemini returned empty metadata.")

    try:
        parsed = json.loads(response_text)
    except ValueError as exc:
        raise RuntimeError("Gemini metadata response was not valid JSON.") from exc

    return normalize_metadata(parsed, title, context, language, mode)


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

    package_text = (
        "TITLE\n"
        + metadata["title"]
        + "\n\nDESCRIPTION\n"
        + metadata["description"]
        + "\n\nTAGS\n"
        + ", ".join(metadata["tags"])
        + "\n\nLANGUAGE\n"
        + metadata["language"]
        + "\n"
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
    ai_story = read_json(AI_STORY_FILE)
    source_story = read_json(STORY_FILE)

    # Use AI story when complete; otherwise use the source story.
    context = story_context(ai_story)
    story_for_title = ai_story if context else source_story

    if not context:
        context = story_context(source_story)

    if not context:
        context = clean(
            values.get("STORY_TEXT") or values.get("STORY") or "",
            MAX_CONTEXT,
        )

    explicit_title = clean(values.get("TITLE") or values.get("TOPIC"))
    title = derive_title(story_for_title, explicit_title, context)

    language = clean(
        values.get("LANGUAGE") or values.get("CAPTIONS") or "Hindi",
        40,
    )

    raw_mode = clean(values.get("FORMAT") or "full").lower()
    mode = (
        "short"
        if raw_mode in {"short", "shorts", "vertical", "reels"}
        else "full"
    )

    model = selected_model()
    fingerprint_data = {
        "version": VERSION,
        "input": values,
        "context": context,
        "title": title,
        "model": model,
        "mode": mode,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_data,
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
        and clean(cached.get("title"))
        and clean(cached.get("description"))
        and isinstance(cached.get("tags"), list)
    ):
        log("Matching validated metadata cache found; no API request.")
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
            log("Using local story-specific fallback; no retry.")
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
    log("Tag count:", len(metadata["tags"]))
    log("Output:", PACKAGE_METADATA.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("METADATA GENERATION ERROR:", exc)
        raise SystemExit(1)
