#!/usr/bin/env python3
"""Generate story-grounded Hindi YouTube title, description and tags."""

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

from input_config import load_and_validate, get_format, get_topic, get_parts

INPUT_TOPIC = ROOT / "Input" / "topic.txt"
STORY_FILES = (
    ROOT / "output" / "story" / "ai_story.json",
    ROOT / "output" / "story" / "story.json",
)
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

OUTPUT_DIR = ROOT / "output" / "publish"
OUTPUT_JSON = OUTPUT_DIR / "youtube_metadata.json"
OUTPUT_TITLE = OUTPUT_DIR / "youtube_title.txt"
OUTPUT_DESCRIPTION = OUTPUT_DIR / "youtube_description.txt"
OUTPUT_TAGS = OUTPUT_DIR / "youtube_tags.txt"
OUTPUT_TEXT = ROOT / "output" / "youtube_metadata.txt"
CHECKPOINT = ROOT / "output" / "checkpoints" / "youtube_metadata.json"

VERSION = "youtube-metadata-v21-complete-story-relevance-verified"
MAX_RETRIES = 3
REQUEST_TIMEOUT = 180
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MIN_TAGS = 5
MAX_TAGS = 20
MAX_TAG_LENGTH = 100
MIN_STORY_WORDS = 30

RETRYABLE_HTTP = {408, 409, 425, 429, 500, 502, 503, 504}
PERMANENT_HTTP = {400, 401, 403, 404}

TITLE_KEYS = (
    "final_title",
    "generated_title",
    "story_title",
    "title",
)
SUMMARY_KEYS = (
    "story_summary",
    "summary",
    "synopsis",
    "logline",
    "short_summary",
)
STORY_TEXT_KEYS = (
    "story_text",
    "full_story",
    "complete_story",
    "original_story",
    "content",
    "narration",
    "script",
    "source_text",
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().strip("\"'“”‘’")


def multiline(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.replace("\ufeff", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def word_count(value: str) -> int:
    return len(re.findall(r"\S+", value))


def digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: dict) -> None:
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def read_json(path: Path, required: bool = True) -> dict | None:
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON {path}: {exc}") from exc
        log("Ignoring unreadable optional JSON:", path, exc)
        return None

    if not isinstance(data, dict):
        if required:
            raise RuntimeError(f"Expected JSON object: {path}")
        return None

    return data


def nested_objects(data: dict) -> list[dict]:
    result = [data]
    seen = {id(data)}

    for key in ("story", "metadata", "story_metadata", "result", "data"):
        value = data.get(key)
        if isinstance(value, dict) and id(value) not in seen:
            result.append(value)
            seen.add(id(value))

    return result


def first_text(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = clean(data.get(key))
        if value:
            return value
    return ""


def ordered_parts(story: dict) -> list[dict]:
    parts = story.get("parts")

    if not isinstance(parts, list):
        nested = story.get("story")
        parts = nested.get("parts") if isinstance(nested, dict) else None

    if not isinstance(parts, list):
        return []

    indexed = []

    for index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part record: {index}")

        try:
            number = int(part.get("part", index))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid story part number at index {index}."
            ) from exc

        indexed.append((number, part))

    indexed.sort(key=lambda item: item[0])
    numbers = [number for number, _ in indexed]

    if numbers and numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        raise RuntimeError("Story part numbering is not continuous.")

    return [part for _, part in indexed]


def ordered_scenes(scenes: list) -> list[dict]:
    indexed = []

    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise RuntimeError(f"Invalid scene record: {index}")

        try:
            number = int(
                scene.get(
                    "global_scene_number",
                    scene.get("global_scene", index),
                )
            )
        except (TypeError, ValueError):
            number = index

        indexed.append((number, scene))

    indexed.sort(key=lambda item: item[0])
    numbers = [number for number, _ in indexed]

    if numbers and numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        raise RuntimeError("Global scene numbering is not continuous.")

    return [scene for _, scene in indexed]


def extract_part_narration(parts: list[dict]) -> str:
    sections = []

    for part in parts:
        narration = multiline(
            part.get("narration")
            or part.get("text")
            or part.get("story")
            or part.get("content")
        )

        if narration:
            sections.append(narration)
            continue

        scenes = part.get("scenes")

        if isinstance(scenes, list):
            for scene in ordered_scenes(scenes):
                value = multiline(
                    scene.get("narration")
                    or scene.get("narration_text")
                    or scene.get("text")
                )
                if value:
                    sections.append(value)

    return "\n\n".join(sections).strip()


def extract_top_level_scenes(story: dict) -> str:
    scenes = story.get("scenes")

    if not isinstance(scenes, list):
        nested = story.get("story")
        scenes = nested.get("scenes") if isinstance(nested, dict) else None

    if not isinstance(scenes, list):
        return ""

    sections = []

    for scene in ordered_scenes(scenes):
        value = multiline(
            scene.get("narration")
            or scene.get("narration_text")
            or scene.get("text")
        )
        if value:
            sections.append(value)

    return "\n\n".join(sections).strip()


def extract_complete_story(story: dict) -> tuple[str, str]:
    """Use complete narration; never substitute a first sentence or summary."""

    for obj in nested_objects(story):
        for key in STORY_TEXT_KEYS:
            value = multiline(obj.get(key))
            if word_count(value) >= MIN_STORY_WORDS:
                return value, key

    reconstructed = extract_part_narration(ordered_parts(story))

    if word_count(reconstructed) >= MIN_STORY_WORDS:
        return reconstructed, "ordered_parts_and_scenes"

    reconstructed = extract_top_level_scenes(story)

    if word_count(reconstructed) >= MIN_STORY_WORDS:
        return reconstructed, "ordered_top_level_scenes"

    raise RuntimeError(
        "Complete story narration is missing or too short. "
        "Metadata generation will not use a partial story."
    )


def validate_story_status(story: dict) -> None:
    statuses = [
        clean(obj.get("status")).lower()
        for obj in nested_objects(story)
        if obj.get("status")
    ]

    if statuses and not any(
        status in {"completed", "complete", "success", "done"}
        for status in statuses
    ):
        raise RuntimeError(f"Story generation is not complete: {statuses}")

    for obj in nested_objects(story):
        if obj.get("error"):
            raise RuntimeError("Story JSON contains an error field.")


def load_story() -> tuple[dict, Path]:
    for path in STORY_FILES:
        data = read_json(path, required=False)
        if isinstance(data, dict):
            validate_story_status(data)
            return data, path

    raise RuntimeError(
        "Completed ai_story.json or story.json was not found."
    )


def load_config() -> tuple[dict, str, str, int]:
    if not INPUT_TOPIC.is_file():
        raise RuntimeError("Input/topic.txt is missing.")

    config = load_and_validate(INPUT_TOPIC)

    if not isinstance(config, dict):
        raise RuntimeError("Input configuration is invalid.")

    topic = clean(get_topic(config))
    mode = clean(get_format(config)).lower()

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode!r}")

    parts = get_parts(config)

    if isinstance(parts, bool) or int(parts) < 1:
        raise RuntimeError("Selected PARTS count is invalid.")

    return config, topic, mode, int(parts)


def load_selected_model() -> str:
    data = read_json(MODEL_FILE)

    if data.get("status") != "selected":
        raise RuntimeError(
            "Selected model is not confirmed in selected_model.json."
        )

    model = clean(data.get("model"))

    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(f"Invalid selected model: {model!r}")

    return model


def prepare_story(
    story: dict,
    mode: str,
    configured_parts: int,
) -> dict:
    validate_story_status(story)

    story_mode = clean(story.get("format")).lower()

    if story_mode and story_mode not in {"short", "full"}:
        raise RuntimeError(f"Invalid story format: {story_mode!r}")

    if story_mode and story_mode != mode:
        raise RuntimeError(
            f"Story format mismatch: input={mode}, story={story_mode}."
        )

    story_text, source = extract_complete_story(story)
    parts = ordered_parts(story)

    existing_title = ""
    summary = ""

    for obj in nested_objects(story):
        existing_title = existing_title or first_text(obj, TITLE_KEYS)
        summary = summary or first_text(obj, SUMMARY_KEYS)

    count = word_count(story_text)

    if count < MIN_STORY_WORDS:
        raise RuntimeError(
            f"Story is too short for metadata: {count} words."
        )

    if parts and len(parts) != configured_parts:
        raise RuntimeError(
            f"Story part count mismatch: configured={configured_parts}, "
            f"story={len(parts)}."
        )

    return {
        "text": story_text,
        "source": source,
        "existing_title": existing_title,
        "summary": summary,
        "word_count": count,
        "part_count": len(parts),
        "story_fingerprint": hashlib.sha256(
            story_text.encode("utf-8")
        ).hexdigest(),
    }


def normalize_title(value: Any) -> str:
    title = clean(value).strip("\"'“”‘’")
    title = re.sub(r"\s+", " ", title)

    if not title:
        raise RuntimeError("Generated YouTube title is empty.")

    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS]
        if " " in title:
            title = title.rsplit(" ", 1)[0]
        title = title.rstrip(" ,.!?;:।")

    if not title:
        raise RuntimeError("Generated title cannot fit YouTube limit.")

    return title


def normalize_description(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeError("Generated description must be text.")

    description = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    description = re.sub(r"\n{3,}", "\n\n", description)

    if not description:
        raise RuntimeError("Generated YouTube description is empty.")

    if len(description) > MAX_DESCRIPTION_CHARS:
        description = (
            description[:MAX_DESCRIPTION_CHARS - 3].rstrip() + "..."
        )

    return description


def normalize_tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,;\n]", raw)

    if not isinstance(raw, list):
        raise RuntimeError("Generated tags must be a JSON array.")

    tags = []
    seen = set()

    for item in raw:
        if not isinstance(item, str):
            continue

        tag = re.sub(r"\s+", " ", item).strip(" ,#")
        if not tag:
            continue

        if len(tag) > MAX_TAG_LENGTH:
            tag = tag[:MAX_TAG_LENGTH].rstrip()

        key = tag.casefold()

        if key in seen:
            continue

        seen.add(key)
        tags.append(tag)

        if len(tags) == MAX_TAGS:
            break

    if len(tags) < MIN_TAGS:
        raise RuntimeError(
            f"Only {len(tags)} unique tags; minimum is {MIN_TAGS}."
        )

    return tags


def validate_metadata(
    data: Any,
    topic: str,
    model: str,
    fingerprint: str,
    mode: str,
) -> dict:
    if not isinstance(data, dict):
        raise RuntimeError("Metadata response is not a JSON object.")

    return {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "topic": topic,
        "model": model,
        "title": normalize_title(data.get("title")),
        "description": normalize_description(data.get("description")),
        "tags": normalize_tags(data.get("tags")),
        "input_fingerprint": fingerprint,
    }


def make_fingerprint(
    topic: str,
    story: dict,
    model: str,
    mode: str,
    config: dict,
) -> str:
    relevant_config = {
        "FORMAT": mode,
        "TITLE": config.get("TITLE"),
        "TOPIC": config.get("TOPIC"),
        "PARTS": config.get("PARTS"),
        "PARTS_SELECTED": config.get("PARTS_SELECTED"),
        "AUDIENCE": config.get("AUDIENCE"),
    }

    return digest({
        "version": VERSION,
        "topic": topic,
        "story_text": story["text"],
        "story_fingerprint": story["story_fingerprint"],
        "existing_title": story["existing_title"],
        "summary": story["summary"],
        "story_source": story["source"],
        "model": model,
        "format": mode,
        "config": relevant_config,
    })


def build_prompt(topic: str, story: dict, mode: str) -> str:
    if mode == "short":
        format_instruction = """
यह केवल YouTube SHORT वीडियो है।
Title और Description में इसे Short के रूप में ही प्रस्तुत करें।
Full movie, full-length video या लंबी फिल्म न लिखें।
"""
    else:
        format_instruction = """
यह FULL-LENGTH कहानी वीडियो है।
इसे YouTube Short न बताएं।
पूरी कहानी के केंद्रीय संघर्ष और उसके परिणाम का सही परिचय दें।
"""

    return f"""
आप Katha Lok AI के Hindi YouTube metadata editor हैं।
पूरी कहानी आरंभ से अंत तक पढ़कर ही metadata बनाएं।

USER TOPIC:
{topic or "उपलब्ध नहीं"}

EXISTING STORY TITLE:
{story["existing_title"] or "उपलब्ध नहीं"}

STORY SUMMARY:
{story["summary"] or "अलग summary उपलब्ध नहीं"}

VIDEO FORMAT:
{mode.upper()}

FORMAT INSTRUCTIONS:
{format_instruction}

COMPLETE STORY:
<complete_story>
{story["text"]}
</complete_story>

अनिवार्य नियम:

1. पूरी कहानी आरंभ से अंत तक पढ़ें।
2. केवल पहली पंक्ति, शुरुआती वाक्य या USER TOPIC से Title न बनाएं।
3. पूरी कहानी का सार, मुख्य पात्र, संघर्ष, कारण, turning point और ending समझें।
4. Title कहानी के वास्तविक केंद्रीय विचार और महत्वपूर्ण घटना पर आधारित हो।
5. मौजूदा story title गलत या अधूरा हो तो उसे दोहराने के बजाय बेहतर Title बनाएं।
6. कहानी में मौजूद न होने वाले पात्र, घटनाएं, स्थान, रहस्य या twist न जोड़ें।
7. Description पूरी कहानी के वास्तविक कथानक पर आधारित हो।
8. झूठा निष्कर्ष या कहानी में न मौजूद घटना न लिखें।
9. Ending का अनावश्यक spoiler न दें; मूल संघर्ष स्पष्ट रखें।
10. Hook या suspense का दावा तभी करें जब कहानी में वास्तव में मौजूद हो।
11. Tags केवल कहानी के पात्रों, विषय, genre, घटनाओं, भाषा और वास्तविक विषयवस्तु से संबंधित हों।
12. असंबंधित trending keywords, दूसरे creators, brands, लोकप्रिय फिल्मों या unrelated topics के नाम न जोड़ें।
13. कम से कम 5 और अधिकतम 20 अलग-अलग relevant tags दें।
14. Hindi और जरूरी English search terms का स्वाभाविक मिश्रण रखें।
15. नकली URL, social links, credits, sources या channel details न बनाएं।
16. असंबंधित hashtags न जोड़ें।
17. स्वाभाविक Hindi Devanagari भाषा का प्रयोग करें।
18. Title अधिकतम 100 characters और Description अधिकतम 5000 characters हो।
19. चुने हुए FORMAT से अलग video format का दावा न करें।
20. Description में कहानी का सार और दर्शक को मिलने वाला वास्तविक अनुभव लिखें।
21. केवल JSON लौटाएं; अतिरिक्त टिप्पणी नहीं।

JSON:
{{
  "title": "पूरी कहानी के सार पर आधारित YouTube title",
  "description": "कहानी पर आधारित सटीक YouTube description",
  "tags": [
    "कहानी से संबंधित tag 1",
    "कहानी से संबंधित tag 2",
    "कहानी से संबंधित tag 3",
    "कहानी से संबंधित tag 4",
    "कहानी से संबंधित tag 5"
  ]
}}
"""

def extract_response_text(result: dict) -> str:
    candidates = result.get("candidates")

    if not isinstance(candidates, list) or not candidates:
        feedback = result.get("promptFeedback", {})
        raise RuntimeError(
            f"Gemini returned no candidates: {feedback}"
        )

    candidate = candidates[0]
    finish_reason = clean(candidate.get("finishReason"))

    if finish_reason in {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
    }:
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
            f"Gemini returned empty metadata. "
            f"Finish reason={finish_reason!r}"
        )

    return response


def parse_metadata_json(response: str) -> dict:
    response = response.strip()
    response = re.sub(
        r"^\s*```(?:json)?\s*",
        "",
        response,
        flags=re.IGNORECASE,
    )
    response = re.sub(r"\s*```\s*$", "", response)

    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        start = response.find("{")
        end = response.rfind("}")

        if start < 0 or end <= start:
            raise RuntimeError("Gemini response is not valid JSON.")

        try:
            data = json.loads(response[start:end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Gemini metadata JSON parsing failed: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("Gemini metadata must be a JSON object.")

    return data


def generate_metadata(
    api_key: str,
    model: str,
    topic: str,
    story: dict,
    mode: str,
    fingerprint: str,
) -> dict:
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

    prompt = build_prompt(topic, story, mode)
    encoded_model = urllib.parse.quote(model, safe="-._")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{encoded_model}:generateContent"
    )

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        payload = {
            "contents": [
                {"parts": [{"text": prompt}]}
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.25,
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
                f"selected model={model}"
            )

            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                result = json.loads(
                    response.read().decode("utf-8")
                )

            parsed = parse_metadata_json(
                extract_response_text(result)
            )

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

            if exc.code in PERMANENT_HTTP:
                break

            if exc.code not in RETRYABLE_HTTP:
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
            time.sleep(min(2 ** attempt, 12))

    raise RuntimeError(
        f"Metadata generation failed after {MAX_RETRIES} attempts: "
        f"{last_error}"
    )


def build_combined_text(metadata: dict) -> str:
    return (
        f"TITLE\n{metadata['title']}\n\n"
        f"DESCRIPTION\n{metadata['description']}\n\n"
        f"TAGS\n{', '.join(metadata['tags'])}\n"
    )


def verify_saved_text(
    path: Path,
    expected: str,
    label: str,
) -> None:
    if not path.is_file():
        raise RuntimeError(f"Saved {label} file is missing.")

    actual = path.read_text(encoding="utf-8-sig").strip()

    if actual != expected.strip():
        raise RuntimeError(f"Saved {label} verification failed.")


def verify_output_files(metadata: dict) -> None:
    verify_saved_text(
        OUTPUT_TITLE,
        metadata["title"],
        "YouTube title",
    )

    verify_saved_text(
        OUTPUT_DESCRIPTION,
        metadata["description"],
        "YouTube description",
    )

    verify_saved_text(
        OUTPUT_TEXT,
        build_combined_text(metadata),
        "combined metadata",
    )

    if not OUTPUT_TAGS.is_file():
        raise RuntimeError("YouTube tags file is missing.")

    saved_tags = [
        clean(item)
        for item in OUTPUT_TAGS.read_text(
            encoding="utf-8-sig"
        ).splitlines()
        if clean(item)
    ]

    if saved_tags != metadata["tags"]:
        raise RuntimeError("Saved YouTube tags verification failed.")

    saved_json = read_json(OUTPUT_JSON)

    if saved_json != metadata:
        raise RuntimeError(
            "Saved YouTube metadata JSON does not match generated data."
        )

    checkpoint = read_json(CHECKPOINT)

    if checkpoint.get("status") != "completed":
        raise RuntimeError("YouTube metadata checkpoint is incomplete.")

    if checkpoint.get("input_fingerprint") != metadata["input_fingerprint"]:
        raise RuntimeError("Metadata checkpoint fingerprint mismatch.")

    expected_hash = digest(metadata)

    if checkpoint.get("metadata_sha256") != expected_hash:
        raise RuntimeError("Metadata checkpoint SHA256 mismatch.")


def save_outputs(metadata: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)

    validated = validate_metadata(
        metadata,
        metadata["topic"],
        metadata["model"],
        metadata["input_fingerprint"],
        metadata["format"],
    )

    # Write individual files before marking the checkpoint complete.
    write_json(OUTPUT_JSON, validated)
    atomic_write(
        OUTPUT_TITLE,
        validated["title"] + "\n",
    )
    atomic_write(
        OUTPUT_DESCRIPTION,
        validated["description"] + "\n",
    )
    atomic_write(
        OUTPUT_TAGS,
        "\n".join(validated["tags"]) + "\n",
    )
    atomic_write(
        OUTPUT_TEXT,
        build_combined_text(validated),
    )

    checkpoint = {
        "status": "completed",
        "version": VERSION,
        "input_fingerprint": validated["input_fingerprint"],
        "metadata_sha256": digest(validated),
        "output_files": {
            "json": OUTPUT_JSON.relative_to(ROOT).as_posix(),
            "title": OUTPUT_TITLE.relative_to(ROOT).as_posix(),
            "description": OUTPUT_DESCRIPTION.relative_to(ROOT).as_posix(),
            "tags": OUTPUT_TAGS.relative_to(ROOT).as_posix(),
            "combined": OUTPUT_TEXT.relative_to(ROOT).as_posix(),
        },
    }

    write_json(CHECKPOINT, checkpoint)
    verify_output_files(validated)


def load_cache(
    fingerprint: str,
    topic: str,
    model: str,
    mode: str,
) -> dict | None:
    data = read_json(OUTPUT_JSON, required=False)

    if not isinstance(data, dict):
        return None

    try:
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

        metadata = validate_metadata(
            data,
            topic,
            model,
            fingerprint,
            mode,
        )

        verify_output_files(metadata)
        return metadata

    except Exception as exc:
        log("Existing metadata cache rejected:", exc)
        return None


def main() -> None:
    config, topic, mode, configured_parts = load_config()

    story_data, story_path = load_story()

    prepared = prepare_story(
        story_data,
        mode,
        configured_parts,
    )

    model = load_selected_model()

    fingerprint = make_fingerprint(
        topic=topic,
        story=prepared,
        model=model,
        mode=mode,
        config=config,
    )

    metadata = load_cache(
        fingerprint,
        topic,
        model,
        mode,
    )

    if metadata is not None:
        log("Using matching verified metadata cache.")
        log("Gemini API calls: 0")

    else:
        api_key = (
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )

        if not api_key:
            raise RuntimeError(
                "Gemini API key is missing and no matching "
                "verified metadata cache exists."
            )

        log("Generating metadata from complete story.")
        log("Selected model:", model)
        log("Format:", mode)
        log("Story source:", story_path.relative_to(ROOT))
        log("Story text source:", prepared["source"])
        log("Story word count:", prepared["word_count"])
        log("Story part count:", prepared["part_count"])

        metadata = generate_metadata(
            api_key=api_key,
            model=model,
            topic=topic,
            story=prepared,
            mode=mode,
            fingerprint=fingerprint,
        )

    metadata["topic"] = topic
    metadata["model"] = model
    metadata["format"] = mode
    metadata["input_fingerprint"] = fingerprint

    save_outputs(metadata)

    log("=" * 60)
    log("YOUTUBE METADATA: SUCCESS")
    log("Format:", mode)
    log("Title:", metadata["title"])
    log("Description characters:", len(metadata["description"]))
    log("Relevant unique tags:", len(metadata["tags"]))
    log("JSON:", OUTPUT_JSON.relative_to(ROOT))
    log("TITLE:", OUTPUT_TITLE.relative_to(ROOT))
    log("DESCRIPTION:", OUTPUT_DESCRIPTION.relative_to(ROOT))
    log("TAGS:", OUTPUT_TAGS.relative_to(ROOT))
    log("COMBINED:", OUTPUT_TEXT.relative_to(ROOT))
    log("CHECKPOINT:", CHECKPOINT.relative_to(ROOT))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("YOUTUBE METADATA: CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log(
            "YOUTUBE METADATA: FAILED:",
            f"{type(exc).__name__}: {exc}",
        )
        raise SystemExit(1)