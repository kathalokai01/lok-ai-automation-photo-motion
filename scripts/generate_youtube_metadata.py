#!/usr/bin/env python3
"""Generate story-grounded Hindi YouTube metadata with configured LLM fallback."""

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
from select_gemini_model import configured_fallbacks
from llm_provider_client import generate_text as generate_provider_text
from gemini_api_guard import require_gemini_generation
from gemini_http import http_error_body, retry_after_seconds

INPUT_TOPIC = ROOT / "Input" / "topic.txt"
STORY_FILES = (
    ROOT / "output" / "story" / "ai_story.json",
    ROOT / "output" / "story" / "story.json",
)
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"
LLM_PROVIDER_FILE = ROOT / "output" / "config" / "selected_llm_provider.json"

OUTPUT_DIR = ROOT / "output" / "publish"
OUTPUT_JSON = OUTPUT_DIR / "youtube_metadata.json"
OUTPUT_TITLE = OUTPUT_DIR / "youtube_title.txt"
OUTPUT_DESCRIPTION = OUTPUT_DIR / "youtube_description.txt"
OUTPUT_TAGS = OUTPUT_DIR / "youtube_tags.txt"
OUTPUT_TEXT = ROOT / "output" / "youtube_metadata.txt"
CHECKPOINT = ROOT / "output" / "checkpoints" / "youtube_metadata.json"

VERSION = "youtube-metadata-v29-hindi-disclaimer-block"
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

TITLE_KEYS = ("final_title", "generated_title", "story_title", "title")
SUMMARY_KEYS = (
    "story_summary", "summary", "synopsis", "logline", "short_summary"
)
STORY_TEXT_KEYS = (
    "story_text", "full_story", "complete_story", "original_story",
    "content", "narration", "script", "source_text",
)


def log(*values: Any) -> None:
    print(*values, flush=True)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {
        "1", "true", "yes", "on", "enabled"
    }


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
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(
            "Story part numbering must start at 1 and remain continuous."
        )
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
    if numbers and numbers != list(
        range(numbers[0], numbers[0] + len(numbers))
    ):
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
    allowed = {"completed", "complete", "success", "done"}
    invalid = [status for status in statuses if status not in allowed]
    if invalid:
        raise RuntimeError(
            f"Story generation has incomplete/failed nested status values: {invalid}"
        )

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


def load_gemini_order() -> list[str]:
    """Use the saved Gemini model order when billing opt-in and key exist."""
    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    authorized = (
        os.environ.get("ALLOW_GEMINI_GENERATION", "").strip()
        == "I_ACCEPT_POTENTIAL_GEMINI_CHARGES"
    )
    if not api_key:
        log("Gemini skipped: API key is not configured.")
        return []
    if not authorized:
        log("Gemini skipped: explicit billable-generation opt-in is absent.")
        return []

    saved = read_json(MODEL_FILE, required=False) or {}
    if saved.get("status") != "selected":
        raise RuntimeError(
            "Gemini selected_model.json is missing or not selected."
        )

    models = configured_fallbacks(saved)
    if not models:
        raise RuntimeError("Configured Gemini fallback order is empty.")

    normalized = []
    for model in models:
        value = clean(model)
        if not re.fullmatch(r"gemini-[A-Za-z0-9._-]+", value, re.IGNORECASE):
            raise RuntimeError(f"Invalid configured Gemini model: {value!r}.")
        if value not in normalized:
            normalized.append(value)

    saved_order = saved.get("fallback_order")
    if not isinstance(saved_order, list) or not saved_order:
        raise RuntimeError("Saved Gemini fallback_order is missing.")
    saved_normalized = [clean(value) for value in saved_order]
    if saved_normalized != normalized:
        raise RuntimeError(
            "Gemini fallback order differs from the saved configured order."
        )
    if clean(saved.get("model")) != normalized[0]:
        raise RuntimeError("Saved Gemini primary model is not first in its order.")
    return normalized


def load_configured_provider_order() -> list[dict]:
    """Read only provider records explicitly marked ready by the selector."""
    data = read_json(LLM_PROVIDER_FILE, required=False)
    if not isinstance(data, dict):
        log("No selected non-Gemini provider configuration found.")
        return []

    if (
        data.get("enabled") is not True
        or data.get("status") != "ready"
    ):
        log("No non-Gemini provider is marked ready.")
        return []

    records = data.get("providers")
    order = data.get("provider_order")
    if not isinstance(records, list) or not isinstance(order, list):
        raise RuntimeError("Selected provider configuration is malformed.")

    supported = {"openai", "groq", "openrouter", "cloudflare"}
    by_name = {}
    for item in records:
        if not isinstance(item, dict):
            raise RuntimeError("Provider selector contains a non-object record.")
        name = clean(item.get("provider")).lower()
        if name not in supported:
            raise RuntimeError(f"Unsupported provider in selector: {name!r}.")
        if name in by_name:
            raise RuntimeError(f"Duplicate provider selector record: {name}.")
        by_name[name] = item

    result = []
    seen = set()
    for raw_provider in order:
        provider = clean(raw_provider).lower()
        if provider not in supported:
            raise RuntimeError(
                f"Unsupported provider in configured order: {provider!r}."
            )
        if provider in seen:
            raise RuntimeError(
                f"Duplicate provider in configured order: {provider}."
            )
        seen.add(provider)
        item = by_name.get(provider)
        if not isinstance(item, dict) or item.get("ready") is not True:
            continue
        model = clean(item.get("model"))
        if not model or len(model) > 200 or any(ch.isspace() for ch in model):
            raise RuntimeError(f"Invalid configured model for {provider}.")
        if provider == "cloudflare" and not model.startswith("@cf/"):
            raise RuntimeError("Cloudflare model must use the @cf/ namespace.")
        result.append({"provider": provider, "model": model})

    if data.get("status") == "ready" and not result:
        log("Selector marked ready but no provider in its configured order is ready.")
    return result


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
    if mode == "short" and story.get("format") == "full":
        raise RuntimeError("FULL story cannot produce SHORT metadata.")
    if mode == "full" and story.get("format") == "short":
        raise RuntimeError("SHORT story cannot produce FULL metadata.")

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
        raise RuntimeError(
            "Canonical story title exceeds YouTube 100-character limit; "
            "it will not be silently shortened or replaced."
        )
    return title


def normalize_description(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeError("Generated description must be text.")
    description = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    description = re.sub(r"\n{3,}", "\n\n", description)
    if not description:
        raise RuntimeError("Generated YouTube description is empty.")

    # Remove previously generated legal sections before appending the current
    # canonical Hindi notices. Keep the story-written description untouched.
    legal_markers = (
        "\nअस्वीकरण (Disclaimer):",
        "\nAI अस्वीकरण (AI Content Disclaimer):",
        "\nकॉपीराइट सूचना (Copyright Notice):",
        "\nThird-Party Content Notice:",
        "\nतृतीय-पक्ष सामग्री सूचना:",
        "\nदर्शक सूचना (Viewer Notice):",
    )
    cut_positions = [
        description.find(marker)
        for marker in legal_markers
        if description.find(marker) >= 0
    ]
    if cut_positions:
        description = description[:min(cut_positions)].rstrip()

    year = time.strftime("%Y", time.gmtime())
    legal_block = (
        "AI अस्वीकरण (AI Content Disclaimer): इस वीडियो के निर्माण में "
        "कहानी/Script, Images, Voiceover, Background Music और Photo Motion "
        "जैसे कुछ हिस्सों के लिए AI Tools का उपयोग किया जा सकता है। अंतिम "
        "रचनात्मक चयन और प्रस्तुति चैनल द्वारा तैयार की जाती है। AI से बने "
        "दृश्य वास्तविक घटनाओं का प्रमाण नहीं हैं।"
        "\n\n"
        "कहानी संबंधी अस्वीकरण (Story Disclaimer): इस वीडियो की कहानी "
        "काल्पनिक, रचनात्मक या नाटकीय रूपांतरण हो सकती है। पात्रों, स्थानों "
        "और घटनाओं का किसी वास्तविक व्यक्ति, संस्था या घटना से संबंध "
        "संयोगवश हो सकता है, जब तक स्पष्ट रूप से अन्यथा न बताया गया हो।"
        "\n\n"
        "कॉपीराइट सूचना (Copyright Notice): © "
        + year
        + " Katha Lok AI. सर्वाधिकार सुरक्षित। इस चैनल की मूल Script, "
        "Narration, Editing और स्वयं निर्मित वीडियो सामग्री का अनधिकृत "
        "Re-upload, Copy, Distribution या व्यावसायिक उपयोग बिना लिखित "
        "अनुमति के प्रतिबंधित है।"
        "\n\n"
        "Third-Party Content Notice (अन्य सामग्री की सूचना): यदि इस वीडियो "
        "में Third-Party Music, Image, Sound Effect या अन्य सामग्री का "
        "उपयोग किया गया है, तो उस सामग्री के संबंधित मालिकों के अधिकार "
        "सुरक्षित रहेंगे। आवश्यकतानुसार स्रोत और License के अनुसार Credit "
        "दिया जाएगा।"
        "\n\n"
        "दर्शक सूचना (Viewer Notice): यह वीडियो मनोरंजन के उद्देश्य से "
        "प्रस्तुत किया गया है। कहानी में दिखाई गई घटनाओं या परिस्थितियों "
        "को वास्तविक जीवन में अपनाने की सलाह न समझें।"
    )

    description = description.rstrip() + "\n\n" + legal_block
    if len(description) > MAX_DESCRIPTION_CHARS:
        reserve = len(legal_block) + 2
        story_limit = max(0, MAX_DESCRIPTION_CHARS - reserve - 3)
        story_part = description[:-(len(legal_block) + 2)].rstrip()
        if len(story_part) > story_limit:
            story_part = story_part[:story_limit].rstrip() + "..."
        description = story_part + "\n\n" + legal_block
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise RuntimeError("Hindi disclaimer block exceeds description limit.")
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
    if any(len(tag) > MAX_TAG_LENGTH for tag in tags):
        raise RuntimeError("A metadata tag exceeds the character limit.")
    return tags


def validate_metadata(
    data: Any,
    topic: str,
    model: str,
    fingerprint: str,
    mode: str,
    provider: str = "gemini",
) -> dict:
    if not isinstance(data, dict):
        raise RuntimeError("Metadata response is not a JSON object.")
    if mode not in {"short", "full"}:
        raise RuntimeError(f"Invalid metadata format: {mode!r}.")
    if not clean(topic):
        raise RuntimeError("Metadata topic cannot be empty.")
    if not clean(model):
        raise RuntimeError("Metadata model cannot be empty.")
    if not re.fullmatch(r"[a-f0-9]{64}", clean(fingerprint)):
        raise RuntimeError("Metadata input fingerprint is invalid.")
    provider = clean(provider).lower()
    if provider not in {"gemini", "openai", "groq", "openrouter", "cloudflare"}:
        raise RuntimeError(f"Unsupported metadata provider: {provider!r}.")
    return {
        "status": "completed",
        "version": VERSION,
        "format": mode,
        "topic": clean(topic),
        "provider": provider,
        "model": clean(model),
        # This validator receives the model response, not a story object.
        # The old undefined story reference raised NameError for every provider.
        "title": normalize_title(data.get("title")),
        "description": normalize_description(data.get("description")),
        "tags": normalize_tags(data.get("tags")),
        "input_fingerprint": fingerprint,
    }


def enforce_canonical_story_title(metadata: dict, story: dict) -> dict:
    """Keep publishing metadata bound to the validated story title."""
    canonical_title = clean(story.get("existing_title"))
    if not canonical_title:
        raise RuntimeError("Canonical story title is missing; metadata cannot be published.")
    result = dict(metadata)
    result["title"] = normalize_title(canonical_title)
    return result


def make_fingerprint(
    topic: str,
    story: dict,
    model_order: list[str],
    provider_order: list[dict],
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
        "gemini_order": model_order,
        "provider_order": provider_order,
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
3. मुख्य पात्र, संघर्ष, कारण, turning point और ending समझें।
4. Title के लिए मौजूदा story title ही canonical title है।
5. YouTube Title में वही canonical story title अक्षरशः रखें; #Shorts या अतिरिक्त SEO शब्द Title में न जोड़ें।
6. कहानी में न मौजूद पात्र, घटना, स्थान या twist न जोड़ें।
7. Description वास्तविक कथानक पर आधारित हो।
8. झूठा निष्कर्ष या कहानी में न मौजूद घटना न लिखें।
9. Ending का अनावश्यक spoiler न दें।
10. Hook या suspense का दावा तभी करें जब कहानी में मौजूद हो।
11. Tags केवल पात्रों, विषय, genre, घटनाओं और वास्तविक विषयवस्तु से संबंधित हों।
12. असंबंधित trending keywords, creators, brands या फिल्मों के नाम न जोड़ें।
13. कम से कम 5 और अधिकतम 20 अलग-अलग relevant tags दें।
14. Hindi और जरूरी English search terms का स्वाभाविक मिश्रण रखें।
15. नकली URL, social links, credits या channel details न बनाएं।
16. स्वाभाविक Hindi Devanagari भाषा का प्रयोग करें।
17. Title अधिकतम 100 और Description अधिकतम 5000 characters हो।
18. चुने हुए FORMAT से अलग video format का दावा न करें।
19. केवल JSON लौटाएं; अतिरिक्त टिप्पणी नहीं।

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


def parse_metadata_json(response: str) -> dict:
    if not isinstance(response, str) or not response.strip():
        raise RuntimeError("Model response is empty or not text.")
    if len(response) > 100_000:
        raise RuntimeError("Model metadata response exceeds 100 KB.")
    response = response.strip()
    response = re.sub(
        r"^\s*```(?:json)?\s*", "", response, flags=re.IGNORECASE
    )
    response = re.sub(r"\s*```\s*$", "", response)

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError(f"Duplicate metadata JSON key: {key!r}.")
            result[key] = value
        return result

    def reject_nonfinite(value):
        raise RuntimeError(f"Invalid non-finite JSON number: {value}.")

    options = {
        "object_pairs_hook": reject_duplicate_keys,
        "parse_constant": reject_nonfinite,
    }
    try:
        data = json.loads(response, **options)
    except json.JSONDecodeError:
        start = response.find("{")
        end = response.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("Model response is not valid JSON.")
        try:
            data = json.loads(response[start:end + 1], **options)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Metadata JSON parsing failed: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("Metadata response must be a JSON object.")
    allowed = {"title", "description", "tags"}
    if set(data) != allowed:
        raise RuntimeError(
            "Metadata JSON must contain exactly title, description and tags."
        )
    return data


def extract_gemini_text(result: dict) -> str:
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError(
            f"Gemini returned no candidates: {result.get('promptFeedback', {})}"
        )

    candidate = candidates[0]
    finish_reason = clean(candidate.get("finishReason"))
    if finish_reason in {
        "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"
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
            f"Gemini returned empty metadata. Finish reason={finish_reason!r}"
        )
    return response


def validate_metadata_result(
    data: dict,
    topic: str,
    model: str,
    fingerprint: str,
    mode: str,
    provider: str,
) -> dict:
    return validate_metadata(
        data, topic, model, fingerprint, mode, provider
    )

def generate_gemini_metadata(
    api_key: str,
    model: str,
    prompt: str,
    topic: str,
    fingerprint: str,
    mode: str,
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

    encoded_model = urllib.parse.quote(model, safe="-._")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{encoded_model}:generateContent"
    )

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        retry_http_error = None
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.25,
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
            log(
                f"Gemini metadata model {model}; "
                f"attempt {attempt}/{MAX_RETRIES}"
            )
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT
            ) as response:
                result = json.loads(response.read().decode("utf-8"))

            parsed = parse_metadata_json(extract_gemini_text(result))
            return validate_metadata_result(
                parsed, topic, model, fingerprint, mode, "gemini"
            )

        except urllib.error.HTTPError as exc:
            retry_http_error = exc
            last_error = RuntimeError(
                f"Gemini HTTP {exc.code}: "
                f"{http_error_body(exc, 1500)}"
            )
            if exc.code in PERMANENT_HTTP or exc.code not in RETRYABLE_HTTP:
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

        log("Gemini attempt failed:", last_error)
        if attempt < MAX_RETRIES:
            delay = (
                retry_after_seconds(retry_http_error, attempt, default_base=12, maximum=120)
                if retry_http_error is not None
                else min(2 ** attempt, 12)
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Gemini model {model} failed: {last_error}"
    )


def generate_external_metadata(
    provider: str,
    model: str,
    prompt: str,
    topic: str,
    fingerprint: str,
    mode: str,
) -> dict:
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(
                f"Configured LLM fallback {provider}/{model}; "
                f"attempt {attempt}/{MAX_RETRIES}"
            )
            result = generate_provider_text(
                provider=provider,
                model=model,
                prompt=prompt,
                max_tokens=4096,
                temperature=0.25,
                timeout=REQUEST_TIMEOUT,
            )

            if not isinstance(result, dict):
                raise RuntimeError(
                    f"{provider} returned an invalid response object."
                )

            response_text = result.get("text")
            if not isinstance(response_text, str) or not response_text.strip():
                raise RuntimeError(
                    f"{provider} returned empty metadata text."
                )

            parsed = parse_metadata_json(response_text)
            actual_model = clean(result.get("model")) or model
            actual_provider = clean(result.get("provider")) or provider

            if actual_provider != provider:
                raise RuntimeError(
                    "Provider client returned a different provider "
                    "than the one requested."
                )

            return validate_metadata_result(
                parsed,
                topic,
                actual_model,
                fingerprint,
                mode,
                actual_provider,
            )

        except Exception as exc:
            last_error = exc
            log(
                f"Fallback {provider}/{model} failed:",
                f"{type(exc).__name__}: {exc}",
            )

            retryable = getattr(exc, "retryable", None)
            if retryable is False:
                break

            if attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 12))

    raise RuntimeError(
        f"LLM provider {provider}/{model} failed: {last_error}"
    )


def generate_metadata_with_fallback(
    gemini_models: list[str],
    external_providers: list[dict],
    topic: str,
    story: dict,
    mode: str,
    fingerprint: str,
) -> dict:
    prompt = build_prompt(topic, story, mode)
    failures = []

    # Preserve the configured Gemini priority and fallback order.
    # Potentially billable Gemini calls require the exact explicit opt-in.
    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )

    if gemini_models and api_key:
        for model in gemini_models:
            try:
                authorized_key = require_gemini_generation(
                    "YouTube metadata generation", api_key=api_key
                )
                generated = generate_gemini_metadata(
                    api_key=authorized_key,
                    model=model,
                    prompt=prompt,
                    topic=topic,
                    fingerprint=fingerprint,
                    mode=mode,
                )
                return enforce_canonical_story_title(generated, story)
            except Exception as exc:
                failures.append(f"Gemini {model}: {exc}")
                log("Gemini model exhausted; trying next configured model.")

    elif gemini_models and not api_key:
        log("Gemini skipped: API key is not configured.")

    # External providers are attempted only after all permitted Gemini
    # models have been exhausted or Gemini was safely skipped.
    for item in external_providers:
        provider = item["provider"]
        model = item["model"]
        try:
            generated = generate_external_metadata(
                provider=provider,
                model=model,
                prompt=prompt,
                topic=topic,
                fingerprint=fingerprint,
                mode=mode,
            )
            return enforce_canonical_story_title(generated, story)
        except Exception as exc:
            failures.append(f"{provider}/{model}: {exc}")
            log("Provider exhausted; trying next configured fallback.")

    details = "\n".join(f"- {item}" for item in failures)
    raise RuntimeError(
        "All configured LLM metadata providers were exhausted. "
        "No placeholder metadata was created."
        + (f"\nFailures:\n{details}" if details else "")
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
        raise RuntimeError(f"Saved {label} file verification failed.")


def verify_output_files(metadata: dict) -> None:
    verify_saved_text(OUTPUT_TITLE, metadata["title"], "YouTube title")
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
    if checkpoint.get("metadata_sha256") != digest(metadata):
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
        metadata.get("provider", "gemini"),
    )

    write_json(OUTPUT_JSON, validated)
    atomic_write(OUTPUT_TITLE, validated["title"] + "\n")
    atomic_write(
        OUTPUT_DESCRIPTION,
        validated["description"] + "\n",
    )
    atomic_write(
        OUTPUT_TAGS,
        "\n".join(validated["tags"]) + "\n",
    )
    atomic_write(OUTPUT_TEXT, build_combined_text(validated))

    checkpoint = {
        "status": "completed",
        "version": VERSION,
        "input_fingerprint": validated["input_fingerprint"],
        "metadata_sha256": digest(validated),
        "provider": validated["provider"],
        "model": validated["model"],
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
        if data.get("format") != mode:
            return None

        provider = clean(data.get("provider")) or "gemini"
        model = clean(data.get("model"))
        if not model:
            return None

        metadata = validate_metadata(
            data,
            topic,
            model,
            fingerprint,
            mode,
            provider,
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

    gemini_models = load_gemini_order()
    external_providers = load_configured_provider_order()

    if not gemini_models and not external_providers:
        raise RuntimeError(
            "No configured LLM metadata provider is ready. Check API keys, "
            "model settings, provider order, and Gemini billing opt-in."
        )

    fingerprint = make_fingerprint(
        topic=topic,
        story=prepared,
        model_order=gemini_models,
        provider_order=external_providers,
        mode=mode,
        config=config,
    )

    metadata = load_cache(fingerprint, topic, mode)

    if metadata is not None:
        log("Using matching verified metadata cache.")
        log("Metadata provider API calls: 0")
    else:
        log("Generating metadata from complete story.")
        log("Format:", mode)
        log("Story source:", story_path.relative_to(ROOT))
        log("Story text source:", prepared["source"])
        log("Story word count:", prepared["word_count"])
        log("Story part count:", prepared["part_count"])
        log(
            "Configured Gemini models:",
            ", ".join(gemini_models) if gemini_models else "skipped",
        )
        log(
            "Configured LLM fallbacks:",
            ", ".join(
                f"{item['provider']}/{item['model']}"
                for item in external_providers
            ) or "none",
        )

        metadata = generate_metadata_with_fallback(
            gemini_models=gemini_models,
            external_providers=external_providers,
            topic=topic,
            story=prepared,
            mode=mode,
            fingerprint=fingerprint,
        )

    save_outputs(metadata)

    log("=" * 60)
    log("YOUTUBE METADATA: SUCCESS")
    log("Format:", metadata["format"])
    log("Provider:", metadata["provider"])
    log("Model:", metadata["model"])
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