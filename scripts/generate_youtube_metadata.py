
#!/usr/bin/env python3
"""Katha Lok AI — whole-story-grounded YouTube metadata."""

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
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

OUT = ROOT / "output"
PUBLISH = OUT / "publish"
OUTPUT_JSON = PUBLISH / "youtube_metadata.json"
OUTPUT_TITLE = PUBLISH / "youtube_title.txt"
OUTPUT_DESCRIPTION = PUBLISH / "youtube_description.txt"
OUTPUT_TAGS = PUBLISH / "youtube_tags.txt"
CHECKPOINT = PUBLISH / "metadata_checkpoint.json"
PACKAGE_METADATA = OUT / "youtube_metadata.txt"

VERSION = "metadata-v11-whole-story-title-grounding"
MAX_TITLE = 100
MAX_DESCRIPTION = 1000
MAX_TAGS = 20
REQUEST_TIMEOUT = 60


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


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, data):
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def read_input():
    if not INPUT_FILE.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    values = {}
    current_key = None

    for raw in INPUT_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.rstrip()

        if not line.strip() or line.lstrip().startswith("#"):
            continue

        if "=" in line:
            key, value = line.split("=", 1)
            current_key = key.strip().upper()
            values[current_key] = value.strip().strip("\"'")
        elif current_key in {"STORY_TEXT", "FULL_STORY", "STORY"}:
            values[current_key] += "\n" + line.strip()

    return values


def collect_story_text(value, depth=0):
    """Collect narrative fields without including visual prompts."""
    if depth > 15:
        return []

    if isinstance(value, str):
        text = value.strip()
        return [text] if len(text) >= 15 else []

    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(collect_story_text(item, depth + 1))
        return result

    if not isinstance(value, dict):
        return []

    result = []

    narrative_keys = (
        "source_text",
        "full_story",
        "story_text",
        "narration",
        "dialogue",
        "ending",
        "summary",
        "synopsis",
        "logline",
        "story",
    )

    for key in narrative_keys:
        item = value.get(key)
        if isinstance(item, str) and len(item.strip()) >= 15:
            result.append(item.strip())

    for key in ("parts", "scenes", "chapters", "story_parts"):
        item = value.get(key)
        if isinstance(item, (dict, list)):
            result.extend(collect_story_text(item, depth + 1))

    return result


def story_context(value):
    candidates = collect_story_text(value)
    result = []
    seen = set()

    for item in candidates:
        key = clean(item).casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(item.strip())

    # Do not truncate the ending of a long story.
    return "\n\n".join(result)


def explicit_story_title(story):
    if not isinstance(story, dict):
        return ""

    for key in ("title", "story_title", "generated_title"):
        value = clean(story.get(key), MAX_TITLE)
        if value:
            return value

    return ""


def fallback_title(story, context, user_title):
    if user_title:
        return clean(user_title, MAX_TITLE)

    # A generated title attached to a story object may be stale or weak.
    # Let the configured model choose a new title from the whole narrative.
    if context:
        return ""

    return explicit_story_title(story) or "हिंदी कहानी"


def selected_model():
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict):
        return ""

    for key in ("model", "selected_model", "model_name", "name"):
        value = clean(data.get(key), 150)
        if value:
            return value

    return ""


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
    # Prefer narrative excerpts from different parts of the story.
    sentences = [
        clean(part)
        for part in re.split(r"(?<=[.!?।])\s+", context)
        if clean(part)
    ]

    excerpts = []
    if sentences:
        positions = sorted({
            0,
            len(sentences) // 3,
            (2 * len(sentences)) // 3,
            len(sentences) - 1,
        })

        for index in positions:
            sentence = sentences[index]
            if sentence not in excerpts:
                excerpts.append(sentence[:300])

    excerpt = clean(" ".join(excerpts), 650)
    description = f"{title}\n\n{excerpt}" if excerpt else title

    if mode == "short":
        description += "\n\n#HindiShorts #HindiKahani"
    else:
        description += "\n\n#HindiKahani #HindiStory"

    return {
        "title": clean(title, MAX_TITLE) or "हिंदी कहानी",
        "description": clean(description, MAX_DESCRIPTION),
        "tags": infer_tags(title, context, mode),
        "language": clean(language, 40) or "Hindi",
    }


def normalize_metadata(data, fallback, context, language, mode):
    if not isinstance(data, dict):
        raise ValueError("Metadata response must be a JSON object.")

    title = clean(data.get("title"), MAX_TITLE) or fallback
    if not title:
        title = "हिंदी कहानी"

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
        if not isinstance(value, str):
            continue

        tag = clean(value, 100).strip(" ,#")
        if tag and tag.casefold() not in seen:
            tags.append(tag)
            seen.add(tag.casefold())

        if len(tags) >= MAX_TAGS:
            break

    if len(tags) < 4:
        tags = local["tags"]

    return {
        "title": title,
        "description": description,
        "tags": tags[:MAX_TAGS],
        "language": clean(data.get("language") or language, 40) or "Hindi",
    }


def request_gemini(api_key, model, fallback, context, language, mode):
    if not context:
        raise RuntimeError("Cannot create whole-story metadata without story text.")

    encoded_model = urllib.parse.quote(model, safe="-_.")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + encoded_model + ":generateContent"
    )

    prompt = f"""
Create accurate YouTube metadata after reading the complete story below.

<complete_story>
{context}
</complete_story>

User-provided title, if any: {fallback or "(none)"}
Language: {language}
Selected video format: {mode}

Return valid JSON only:
{{"title":"...","description":"...","tags":["..."],"language":"..."}}

TITLE RULES:
- Read the entire beginning, middle, and ending before selecting a title.
- Base it on the central character, conflict, turning point, or overall meaning.
- Never merely copy the opening sentence.
- Never join random words to form a title.
- Do not invent events, characters, motives, or outcomes.
- If the user provided a title, preserve its central idea.
- If no title was provided, do not blindly reuse an automatically generated
  title from an earlier pipeline stage.
- Do not unnecessarily reveal a major twist.
- Maximum 100 characters.

DESCRIPTION RULES:
- Describe this actual story, its premise, and central conflict.
- Build curiosity without false claims or unrelated generic text.
- Maximum 1000 characters.

TAG RULES:
- Return 8 to 15 relevant Hindi/English tags.
- Tags must match actual story themes and selected format.
- No unrelated trending tags or duplicates.
- Do not claim unsupported genres or events.

Return JSON only, without Markdown fences.
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

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(
            f"Gemini metadata HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"Gemini metadata request failed: {exc}"
        ) from exc

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
        raise RuntimeError("Gemini returned empty metadata.")

    try:
        parsed = json.loads(response_text)
    except ValueError as exc:
        raise RuntimeError("Gemini metadata was not valid JSON.") from exc

    return normalize_metadata(parsed, fallback, context, language, mode)


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
    atomic_write(OUTPUT_TITLE, metadata["title"] + "\n")
    atomic_write(OUTPUT_DESCRIPTION, metadata["description"] + "\n")
    atomic_write(OUTPUT_TAGS, ", ".join(metadata["tags"]) + "\n")

    package_text = (
        "TITLE\n" + metadata["title"]
        + "\n\nDESCRIPTION\n" + metadata["description"]
        + "\n\nTAGS\n" + ", ".join(metadata["tags"])
        + "\n\nLANGUAGE\n" + metadata["language"] + "\n"
    )
    atomic_write(PACKAGE_METADATA, package_text)

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

    # Explicit story text takes priority over downstream generated summaries.
    supplied_text = (
        values.get("STORY_TEXT")
        or values.get("FULL_STORY")
        or values.get("STORY")
        or ""
    ).strip()

    if supplied_text:
        story_for_title = {}
        context = supplied_text
        story_source = "Input/topic.txt"
    else:
        ai_context = story_context(ai_story)
        source_context = story_context(source_story)

        if ai_context:
            story_for_title = ai_story
            context = ai_context
            story_source = "ai_story.json"
        elif source_context:
            story_for_title = source_story
            context = source_context
            story_source = "story.json"
        else:
            story_for_title = {}
            context = ""
            story_source = "none"

    user_title = clean(values.get("TITLE"), MAX_TITLE)

    # TOPIC may be the requested subject, not a verified final story title.
    topic = clean(values.get("TOPIC"), MAX_TITLE)
    if not user_title and topic and not supplied_text:
        user_title = topic

    title_fallback = fallback_title(
        story_for_title,
        context,
        user_title,
    )

    language = clean(
        values.get("LANGUAGE") or values.get("CAPTIONS") or "Hindi",
        40,
    )

    raw_mode = clean(values.get("FORMAT") or "full").lower()
    if raw_mode in {"short", "shorts", "vertical", "reels"}:
        mode = "short"
    elif raw_mode in {"full", "long", "long-form"}:
        mode = "full"
    else:
        raise RuntimeError(
            f"Unsupported FORMAT={raw_mode!r}; expected short or full."
        )

    model = selected_model()

    fingerprint_data = {
        "version": VERSION,
        "input": values,
        "context": context,
        "fallback_title": title_fallback,
        "model": model,
        "mode": mode,
        "story_source": story_source,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_data,
            ensure_ascii=False,
            sort_keys=True,
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
        log("Matching metadata cache found; no API request.")
        save_outputs(cached, fingerprint, "cache")
        return

    # Local fallback remains available when the API key/model is unavailable.
    local_title = title_fallback or explicit_story_title(story_for_title)
    if not local_title:
        local_title = "हिंदी कहानी"

    local = make_local_metadata(
        local_title,
        context,
        language,
        mode,
    )

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if api_key and model and context:
        try:
            metadata = request_gemini(
                api_key,
                model,
                title_fallback,
                context,
                language,
                mode,
            )
            source = "gemini_whole_story"
        except RuntimeError as exc:
            log("WARNING:", exc)
            log("Using local metadata fallback; no automatic retry.")
            metadata = local
            source = "local_fallback"
    else:
        log("Gemini key/model/story unavailable; using local metadata.")
        metadata = local
        source = "local_fallback"

    save_outputs(metadata, fingerprint, source)

    log("METADATA GENERATION: SUCCESS")
    log("Source:", source)
    log("Story source:", story_source)
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
