#!/usr/bin/env python3
"""Katha Lok AI — complete Hindi story correction, segmentation and scene mapping."""

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

from generate_story_plan import (
    build_plan as build_story_plan,
    validate_plan as validate_generated_plan,
)
from input_config import (
    load_and_validate,
    get_format,
    get_topic,
    get_story_text,
    get_parts,
    get_scenes,
)
from story_length_policy import (
    parse_parts_range,
    select_final_part_count,
)

STORY_DIR = ROOT / "output" / "story"
PLAN_FILE = STORY_DIR / "story_plan.json"
OUTPUT_FILE = STORY_DIR / "story.json"
CACHE_FILE = STORY_DIR / "story_cache.json"
TITLE_FILE = STORY_DIR / "final_title.txt"
POLICY_FILE = STORY_DIR / "story_length_policy.json"
INPUT_FILE = ROOT / "Input" / "topic.txt"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

VERSION = "story-builder-v28-complete-hindi-correction-and-safe-segmentation"

SHORT_MIN_WORDS = 120
SHORT_TARGET_WORDS = 135
SHORT_MAX_WORDS = 150
SHORT_HARD_MAX_WORDS = 180
SHORT_REPAIR_ATTEMPTS = 2

MAX_RESPONSE_TOKENS = 16000
REQUEST_TIMEOUT = 180
MAX_RETRIES = 2
RETRY_DELAYS = (5, 12)
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}

STOP_WORDS = set("""
और या लेकिन मगर क्योंकि इसलिए फिर तब जब जहाँ वहाँ जिसने जिसका
उसने उसका उसकी उसके उनके उनकी यह वह ये वे इस उस एक कोई कुछ सभी
बहुत काफी था थी थे है हैं हो हुआ हुई हुए कर करके किया किए की का
के में पर से तक लिए साथ अपने अपनी अपना नहीं नही भी ही तो अब आज
कल वहां यहां रहा रही रहे बाद पहले सामने दौरान तरह बात कहानी कथा
इन इसे उसे तथा अथवा केवल जबकि जिससे जिसका जिसके जिनकी जिनका
""".split())


def log(*values):
    print(*values, flush=True)


def clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_story_text(value: Any) -> str:
    text = str(value or "").replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def word_count(value: Any) -> int:
    return len(re.findall(r"\S+", clean(str(value or ""))))


def digest(data: Any) -> str:
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def atomic_write(path: str | Path, content: str) -> None:
    path = Path(path)
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


def write_json(path: str | Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: str | Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def read_story_text_direct(path: str | Path) -> str:
    """Read the complete multiline STORY_TEXT assignment."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(str(file_path))

    lines = file_path.read_text(encoding="utf-8-sig").splitlines()

    for index, line in enumerate(lines):
        match = re.match(r"^\s*STORY_TEXT\s*=\s*(.*?)\s*$", line)
        if not match:
            continue

        value = match.group(1).strip()
        delimiter = (
            '"""' if value.startswith('"""')
            else "'''" if value.startswith("'''")
            else None
        )

        if delimiter:
            remainder = value[len(delimiter):]
            end = remainder.find(delimiter)
            if end >= 0:
                return remainder[:end].strip()

            collected = [remainder] if remainder else []
            for current in lines[index + 1:]:
                end = current.find(delimiter)
                if end >= 0:
                    if current[:end]:
                        collected.append(current[:end])
                    return "\n".join(collected).strip()
                collected.append(current)

            raise RuntimeError("STORY_TEXT multiline block has no closing delimiter.")

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]

        return value

    return ""


def get_complete_source(config: dict, input_path: str | Path) -> str:
    parsed = get_story_text(config)
    direct = read_story_text_direct(input_path)

    if word_count(direct) > word_count(parsed):
        log("Using complete direct STORY_TEXT input:", word_count(direct), "words")
        return normalize_story_text(direct)

    return normalize_story_text(parsed or direct)


def split_sentences(text: str) -> list[str]:
    """Preserve punctuation and do not cut inside a sentence."""
    result = []
    for block in re.split(r"\n+", normalize_story_text(text)):
        block = block.strip()
        if not block:
            continue
        pieces = re.split(r"(?<=[।.!?！？])\s+", block)
        result.extend(clean(piece) for piece in pieces if clean(piece))
    return result


def split_clauses(sentence: str) -> list[str]:
    sentence = clean(sentence)
    if not sentence:
        return []
    return [
        clean(piece)
        for piece in re.split(r"(?<=[,，;:—–])\s+", sentence)
        if clean(piece)
    ]


def _balanced_units(units: list[str], count: int) -> list[str]:
    if count < 1 or len(units) < count:
        raise RuntimeError("Not enough complete narration units for requested split.")

    groups = []
    cursor = 0

    for index in range(count):
        remaining_groups = count - index
        if remaining_groups == 1:
            groups.append(" ".join(units[cursor:]))
            break

        maximum = len(units) - cursor - remaining_groups + 1
        remaining_words = sum(word_count(x) for x in units[cursor:])
        target = remaining_words / remaining_groups
        group = []
        amount = 0

        while cursor < len(units) and len(group) < maximum:
            candidate = units[cursor]
            candidate_words = word_count(candidate)

            if group and abs(amount - target) <= abs(
                amount + candidate_words - target
            ):
                break

            group.append(candidate)
            amount += candidate_words
            cursor += 1

            if amount >= target:
                break

        if not group:
            group.append(units[cursor])
            cursor += 1

        groups.append(" ".join(group))

    if len(groups) != count or any(not clean(x) for x in groups):
        raise RuntimeError("Could not create the exact requested non-empty groups.")

    return groups


def split_balanced(text: str, count: int) -> list[str]:
    """
    Split by complete sentences first, natural clauses second.
    Never use arbitrary word boundaries to manufacture a sentence break.
    """
    if count < 1:
        raise RuntimeError("Group count must be positive.")

    sentences = split_sentences(text)
    if not sentences:
        raise RuntimeError("Story has no usable sentences.")

    if len(sentences) >= count:
        units = sentences
    else:
        units = []
        for sentence in sentences:
            units.extend(split_clauses(sentence))

    if len(units) < count:
        raise RuntimeError(
            "The story has fewer natural sentence/clause boundaries than "
            f"the requested {count} parts. Refusing to cut mid-sentence."
        )

    groups = _balanced_units(units, count)

    original_words = word_count(" ".join(units))
    split_words = sum(word_count(group) for group in groups)
    if original_words != split_words:
        raise RuntimeError("Segmentation changed the narration word count.")

    return groups


def derive_title_from_story(text: str) -> str:
    """Use themes from the complete story, including its middle and ending."""
    sentences = split_sentences(text)
    if not sentences:
        return "हिंदी कहानी"

    weights = {}
    first_seen = {}
    total = len(sentences)

    for index, sentence in enumerate(sentences):
        tokens = set(re.sub(r"[^\w\s]", " ", sentence.lower()).split())
        position = index / max(total - 1, 1)
        weight = 1.5 if position >= 0.75 else 1.2 if position >= 0.20 else 1.0

        for token in tokens:
            if len(token) <= 2 or token in STOP_WORDS or token.isdigit():
                continue
            weights[token] = weights.get(token, 0.0) + weight
            first_seen.setdefault(token, index)

    if not weights:
        return "हिंदी कहानी"

    ranked = sorted(
        weights,
        key=lambda token: (-weights[token], first_seen[token], token),
    )
    title = " ".join(ranked[:5]).strip()
    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0]
    return title or "हिंदी कहानी"


def selected_model() -> str:
    data = read_json(MODEL_FILE)
    model = clean(data.get("model")) if data else ""
    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError("Selected Gemini model is missing or invalid.")
    return model


def gemini_api_key() -> str:
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )


class GeminiRequestError(RuntimeError):
    def __init__(self, message: str, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def extract_json_object(text: str) -> dict | None:
    value = str(text or "").strip()
    if not value:
        return None

    value = re.sub(
        r"^\s*```(?:json)?\s*|\s*```\s*$",
        "", value, flags=re.IGNORECASE,
    ).strip()

    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(value):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(value[index:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def parse_model_story_response(response_text: str) -> dict:
    parsed = extract_json_object(response_text)
    if parsed is not None:
        return parsed

    plain = normalize_story_text(response_text)
    plain = re.sub(
        r"^\s*(?:कहानी|शीर्षक|भाग\s*\d+)\s*[:：-]\s*",
        "", plain, flags=re.IGNORECASE,
    )
    if not plain:
        raise GeminiRequestError("Model returned neither usable JSON nor story text.")

    return {
        "title": "",
        "story_text": plain,
        "parts": [],
        "response_format": "plain_text",
    }


def call_gemini_once(prompt: str, model: str, api_key: str) -> dict:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model, safe="-._")
        + ":generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.30,
            "maxOutputTokens": MAX_RESPONSE_TOKENS,
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
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise GeminiRequestError(
            f"Gemini HTTP {exc.code}: {body[:1800]}",
            status=exc.code,
            retryable=exc.code in RETRYABLE_HTTP,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GeminiRequestError(f"Gemini network error: {exc}", retryable=True) from exc

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GeminiRequestError("Gemini API returned invalid response JSON.") from exc

    candidates = result.get("candidates", [])
    if not candidates:
        raise GeminiRequestError(
            "Gemini returned no candidate: "
            + json.dumps(result.get("promptFeedback", {}), ensure_ascii=False)
        )

    response_parts = candidates[0].get("content", {}).get("parts", [])
    response_text = "".join(
        part.get("text", "") for part in response_parts if isinstance(part, dict)
    ).strip()

    if not response_text:
        raise GeminiRequestError("Gemini returned an empty response.")

    return parse_model_story_response(response_text)


def call_gemini(prompt: str, model: str, api_key: str) -> dict:
    """Retry the already-selected model; do not reorder or invent providers."""
    errors = []
    for attempt in range(MAX_RETRIES):
        try:
            log(f"Gemini model={model}; attempt={attempt + 1}/{MAX_RETRIES}")
            result = call_gemini_once(prompt, model, api_key)
            log("Gemini story request succeeded:", model)
            return result
        except GeminiRequestError as exc:
            errors.append(str(exc))
            log("Gemini request failed:", str(exc))
            if not exc.retryable:
                break
            if attempt + 1 < MAX_RETRIES:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])

    raise RuntimeError(
        f"Selected Gemini model {model} failed: " + " | ".join(errors)
    )


def build_generation_prompt(
    mode: str,
    topic: str,
    title: str,
    source_text: str,
    parts_count: int,
    config: dict,
) -> str:
    rules = f"""
FORMAT: {mode}
EXACT PART COUNT: {parts_count}
AUDIENCE: {clean(str(config.get("AUDIENCE", "adult")))}
SHORT WORD RULES:
- Each SHORT part target is 120–150 words.
- A part above 150 words must be revised.
- Absolute hard limit is 180 words.
- Fewer than 120 or more than 180 words is a failure.
FULL RULES:
- Preserve the entire story and its original ending.
- Do not create an additional SHORT version.
PART RULES:
- Read the complete supplied story before editing.
- Correct Hindi matras, spelling, grammar, sentence construction and punctuation.
- Preserve every character, relationship, place, object, clue, event and consequence.
- Preserve chronology, motivations and the original resolution.
- Do not invent events to increase word count.
- Use natural Hindi Devanagari unless the source requires otherwise.
- Keep narration natural and coherent.
- Prefer complete sentence boundaries: पूर्ण विराम, प्रश्नवाचक, विस्मयादिबोधक.
- A comma or clause boundary is a secondary option only when necessary.
- Never cut a sentence mid-way merely to equalize word counts.
- Do not split a phrase from its grammatical completion.
- Each part must have a complete thought and chronological continuity.
- Each part begins with a story-specific hook, not a generic repeated line.
- Each non-final part ends at a genuine story-based suspense point.
- The final part must conclude the actual story and resolve its central conflict.
TITLE RULE:
- Use explicit TITLE if supplied.
- If TITLE is blank and a complete story is supplied, derive title after reading
  the entire story, including its middle and ending.
- Never derive the title only from the first line or opening sentences.
- If only TOPIC is supplied, create a title based on the generated story.
"""
    task = (
        "Correct and rewrite the complete supplied story without changing its meaning."
        if source_text
        else "Generate a complete original Hindi story from the supplied topic/title."
    )

    return f"""
You are a professional Hindi story editor and cinematic storyteller.

TASK: {task}
EXPLICIT TITLE: {title or "(not supplied)"}
TOPIC: {topic or "(not supplied)"}

COMPLETE SOURCE STORY:
<source_story>
{source_text}
</source_story>

{rules}

Return valid JSON only:
{{
  "title": "title derived from the complete story",
  "story_text": "complete corrected story",
  "parts": [
    {{"part": 1, "text": "complete narration"}}
  ]
}}
Part numbers must be sequential. Do not add explanations or markdown fences.
"""


def extract_part_text(row: Any, number: int) -> str:
    if isinstance(row, str):
        text = clean(row)
        if text:
            return text
    if not isinstance(row, dict):
        raise RuntimeError(f"Invalid generated story part {number}.")

    try:
        returned_number = int(row.get("part", number))
    except (TypeError, ValueError):
        returned_number = -1

    if returned_number != number:
        raise RuntimeError(f"Generated part numbering mismatch at part {number}.")

    text = clean(
        row.get("text") or row.get("story")
        or row.get("narration") or row.get("content")
    )
    if not text:
        raise RuntimeError(f"Generated story part {number} is empty.")
    return text


def extract_response_parts(data: dict) -> list[str]:
    rows = data.get("parts")
    if isinstance(rows, list) and rows:
        return [extract_part_text(row, i) for i, row in enumerate(rows, 1)]

    complete = clean(
        data.get("story_text") or data.get("story")
        or data.get("narration") or data.get("text")
    )
    if complete:
        return [complete]

    raise RuntimeError("Model response contains neither story parts nor complete story text.")


def validate_short_parts(parts: list[Any]) -> bool:
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("SHORT story has no parts.")

    errors = []
    for number, item in enumerate(parts, 1):
        text = (
            clean(item.get("text") or item.get("story") or item.get("narration"))
            if isinstance(item, dict) else clean(str(item))
        )
        count = word_count(text)
        if count < SHORT_MIN_WORDS:
            errors.append(f"Part {number}: {count} words, below 120.")
        elif count > SHORT_HARD_MAX_WORDS:
            errors.append(f"Part {number}: {count} words, above hard limit 180.")

    if errors:
        raise RuntimeError("SHORT word-count validation failed: " + " | ".join(errors))
    return True


def repair_short_parts(
    parts: list[Any],
    source_text: str,
    topic: str,
    title: str,
    model: str,
    api_key: str,
) -> list[str]:
    """Repair only invalid parts; retain already-valid narration."""
    if not isinstance(parts, list):
        raise RuntimeError("SHORT parts must be a list.")

    repaired = []
    for index, original in enumerate(parts):
        original_text = (
            clean(original.get("text") or original.get("story") or original.get("narration"))
            if isinstance(original, dict) else clean(str(original))
        )
        count = word_count(original_text)

        if SHORT_MIN_WORDS <= count <= SHORT_MAX_WORDS:
            repaired.append(original_text)
            continue

        instruction = (
            "Reduce to 120–150 words without removing essential events."
            if count > SHORT_MAX_WORDS else
            "Expand to 120–150 words only with relevant details already in the story."
        )

        errors = []
        corrected = None
        for attempt in range(SHORT_REPAIR_ATTEMPTS):
            prompt = f"""
Revise this Hindi story part. Return JSON only: {{"text":"..."}}
Part number: {index + 1}
Current word count: {count}
Required: 120–150 words; absolute maximum 180.
Title: {title or topic}
Complete story context:
{source_text}

Current part:
{original_text}

Instructions:
- {instruction}
- Preserve events, characters, relationships, chronology and continuity.
- Do not invent any event, clue, twist or ending.
- Correct matras, spelling, grammar and punctuation.
- Keep complete sentences and natural emotional thoughts together.
- Do not truncate a sentence to meet the count.
"""
            try:
                result = call_gemini(prompt, model, api_key)
                candidate = clean(
                    result.get("text") or result.get("story")
                    or result.get("narration") or result.get("story_text")
                )
                candidate_count = word_count(candidate)
                if candidate and SHORT_MIN_WORDS <= candidate_count <= SHORT_MAX_WORDS:
                    corrected = candidate
                    break
                errors.append(f"Attempt {attempt + 1}: {candidate_count} words")
            except Exception as exc:
                errors.append(f"Attempt {attempt + 1}: {exc}")

        if corrected is None:
            raise RuntimeError(
                f"SHORT part {index + 1} revision failed: " + " | ".join(errors)
            )
        repaired.append(corrected)

    validate_short_parts(repaired)
    return repaired

def generate_narrative(
    config: dict,
    mode: str,
    topic: str,
    title: str,
    source_text: str,
    parts_count: int,
    model: str,
) -> dict:
    api_key = gemini_api_key()

    if not api_key:
        if not source_text:
            raise RuntimeError(
                "Topic/title-only story generation requires GEMINI_API_KEY "
                "or GOOGLE_API_KEY."
            )

        raise RuntimeError(
            "A supplied story requires the configured story model for Hindi "
            "matra, spelling and grammar correction. Offline splitting alone "
            "cannot be treated as proofreading."
        )

    prompt = build_generation_prompt(
        mode, topic, title, source_text, parts_count, config
    )
    data = call_gemini(prompt, model, api_key)
    parts = extract_response_parts(data)

    complete_story = normalize_story_text(
        data.get("story_text") or data.get("story")
        or data.get("narration") or data.get("text")
    )
    if not complete_story:
        complete_story = "\n\n".join(parts)
    if not complete_story:
        raise RuntimeError("Generated complete story is empty.")

    returned_title = clean(data.get("title"))
    final_title = title or returned_title or derive_title_from_story(complete_story)

    return {
        "title": final_title[:180],
        "parts": parts,
        "story_text": complete_story,
        "generation_method": "gemini",
        "model": model,
        "proofreading": {
            "performed": True,
            "method": "configured_gemini_model",
            "complete_story_reviewed": True,
        },
    }


def repair_offline_short_parts(parts: list[str]) -> list[str]:
    """Offline mode cannot proofread or revise narration."""
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("SHORT story has no parts.")
    for index, part in enumerate(parts, 1):
        count = word_count(part)
        if not SHORT_MIN_WORDS <= count <= SHORT_MAX_WORDS:
            raise RuntimeError(
                f"Offline SHORT part {index} has {count} words. "
                "A configured model is required to revise it to 120–150 words."
            )
    return parts


def select_story_parts(
    narrative: dict,
    mode: str,
    parts_specification: Any,
    story_length: Any,
    config: dict,
    provisional_count: int,
    model: str,
) -> tuple[list[str], int, str]:
    """Choose final part count after proofreading the complete story."""
    raw_parts = narrative.get("parts")
    complete_story = normalize_story_text(
        narrative.get("story_text") or narrative.get("story")
        or narrative.get("narration")
    )

    if not complete_story and isinstance(raw_parts, list):
        complete_story = "\n\n".join(
            extract_part_text(row, index)
            for index, row in enumerate(raw_parts, 1)
        )

    if not complete_story:
        raise RuntimeError("Complete corrected story is missing.")

    if mode == "short":
        selected_count = select_final_part_count(
            total_corrected_words=word_count(complete_story),
            format_type="short",
            parts_specification=parts_specification,
            story_length=story_length,
            config=config,
        )
        log("Corrected complete story words:", word_count(complete_story))
        log("Configured PARTS:", parts_specification)
        log("Final selected SHORT parts:", selected_count)

        selected_parts = split_balanced(complete_story, selected_count)
        api_key = gemini_api_key()
        if not api_key:
            selected_parts = repair_offline_short_parts(selected_parts)
        else:
            selected_parts = repair_short_parts(
                selected_parts,
                complete_story,
                clean(get_topic(config)),
                clean(narrative.get("title")),
                model,
                api_key,
            )

        validate_short_parts(selected_parts)
        if len(selected_parts) != selected_count:
            raise RuntimeError("Final SHORT part count changed during revision.")

        return selected_parts, selected_count, complete_story

    minimum, maximum = parse_parts_range(parts_specification)
    selected_count = int(
        config.get("PARTS_SELECTED")
        or config.get("PARTS")
        or provisional_count
    )
    if not minimum <= selected_count <= maximum:
        raise RuntimeError(
            f"FULL selected part count {selected_count} is outside "
            f"configured range {minimum}-{maximum}."
        )

    if isinstance(raw_parts, list) and len(raw_parts) == selected_count:
        selected_parts = [
            extract_part_text(row, index)
            for index, row in enumerate(raw_parts, 1)
        ]
    else:
        selected_parts = split_balanced(complete_story, selected_count)

    if len(selected_parts) != selected_count:
        raise RuntimeError("FULL story part count does not match configuration.")

    return selected_parts, selected_count, complete_story


def make_story(
    config: dict,
    plan: dict,
    narrative: dict,
    source_text: str,
    topic: str,
    mode: str,
    parts_count: int,
    scenes_per_part: int,
) -> dict:
    title = clean(narrative.get("title")) or derive_title_from_story(
        narrative.get("story_text") or source_text
    )
    raw_parts = narrative.get("parts")

    if not isinstance(raw_parts, list) or len(raw_parts) != parts_count:
        raise RuntimeError(
            f"Story part mismatch: expected {parts_count}, "
            f"received {len(raw_parts) if isinstance(raw_parts, list) else 0}."
        )

    plan_parts = plan.get("parts")
    if not isinstance(plan_parts, list) or len(plan_parts) != parts_count:
        raise RuntimeError("Story plan part count does not match final story.")

    story_parts = []
    all_scenes = []
    global_scene = 0

    for part_index, raw_part in enumerate(raw_parts, 1):
        if isinstance(raw_part, dict):
            part_text = extract_part_text(raw_part, part_index)
            part_title = clean(raw_part.get("title"))
        else:
            part_text = clean(str(raw_part))
            part_title = ""

        if not part_text:
            raise RuntimeError(f"Part {part_index} has empty narration.")

        amount = word_count(part_text)
        if mode == "short" and not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
            raise RuntimeError(
                f"SHORT part {part_index} has {amount} words; required 120–150."
            )

        scene_texts = split_balanced(part_text, scenes_per_part)
        if len(scene_texts) != scenes_per_part:
            raise RuntimeError(f"Part {part_index} scene count mismatch.")
        if word_count(" ".join(scene_texts)) != amount:
            raise RuntimeError(f"Scene splitting changed narration in part {part_index}.")

        plan_part = plan_parts[part_index - 1]
        plan_scenes = plan_part.get("scenes") if isinstance(plan_part, dict) else None
        if not isinstance(plan_scenes, list) or len(plan_scenes) != scenes_per_part:
            raise RuntimeError(f"Story plan scenes are invalid for part {part_index}.")

        part_scenes = []
        for scene_index, narration_text in enumerate(scene_texts, 1):
            global_scene += 1
            plan_scene = plan_scenes[scene_index - 1]
            if not isinstance(plan_scene, dict):
                raise RuntimeError(
                    f"Invalid plan scene in part {part_index}, scene {scene_index}."
                )

            scene_id = f"part_{part_index:02d}_scene_{scene_index:02d}"
            anchor = clean(plan_scene.get("story_anchor"))
            purpose = clean(plan_scene.get("purpose"))
            visual = clean(
                plan_scene.get("visual")
                or plan_scene.get("visual_prompt")
                or plan_scene.get("description")
            )

            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene. Show the exact "
                    f"story moment described by this narration: {narration_text}. "
                    f"Story context: {anchor or topic or title}. Include only "
                    "people, objects, locations and actions supported by the story. "
                    "Maintain character identity, age, clothing, setting and chronology. "
                    "Use realistic anatomy, natural expressions and cinematic framing."
                )

            scene_row = {
                "part": part_index,
                "scene": scene_index,
                "scene_id": scene_id,
                "global_scene_number": global_scene,
                "purpose": purpose,
                "role": clean(plan_scene.get("role")) or "main",
                "beat": clean(plan_scene.get("beat")),
                "story_anchor": anchor,
                "narration": narration_text,
                "visual": visual,
                "visual_prompt": visual,
                "dialogue": "",
                "suspense": clean(
                    plan_scene.get("suspense")
                    or plan_scene.get("suspense_prompt")
                    or purpose
                ),
            }
            part_scenes.append(scene_row)
            all_scenes.append(scene_row)

        story_parts.append({
            "part": part_index,
            "part_id": f"part_{part_index:02d}",
            "title": part_title or f"भाग {part_index}",
            "narration": part_text,
            "word_count": amount,
            "scenes": part_scenes,
        })

    story_text = "\n\n".join(part["narration"] for part in story_parts)
    result = {
        "version": VERSION,
        "status": "completed",
        "title": title,
        "final_title": title,
        "topic": topic,
        "format": mode,
        "model": clean(narrative.get("model")) or "offline",
        "generation_method": narrative.get("generation_method", "gemini"),
        "proofreading": narrative.get("proofreading", {
            "performed": False,
            "method": "not_recorded",
        }),
        "source_text": source_text,
        "source_word_count": word_count(source_text),
        "story_text": story_text,
        "total_story_words": word_count(story_text),
        "total_parts": parts_count,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(all_scenes),
        "scene_order": "part_then_scene",
        "parts": story_parts,
        "scenes": all_scenes,
    }

    validate_story(result, parts_count, scenes_per_part, mode)
    return result


def validate_story(
    story: dict,
    expected_parts: int,
    expected_scenes: int,
    mode: str,
) -> bool:
    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("Story output is not a completed JSON object.")
    if not clean(story.get("title")):
        raise RuntimeError("Story title is missing.")
    if clean(story.get("format")).lower() != mode:
        raise RuntimeError("Story format does not match input.")

    parts = story.get("parts")
    scenes = story.get("scenes")
    if not isinstance(parts, list) or len(parts) != expected_parts:
        raise RuntimeError(f"Story part count mismatch: expected {expected_parts}.")
    expected_total = expected_parts * expected_scenes
    if not isinstance(scenes, list) or len(scenes) != expected_total:
        raise RuntimeError(f"Story scene count mismatch: expected {expected_total}.")

    flattened = []
    complete_parts_text = []
    global_index = 0

    for part_index, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != part_index:
            raise RuntimeError(f"Story part numbering mismatch: {part_index}.")

        part_text = clean(part.get("narration"))
        if not part_text:
            raise RuntimeError(f"Part {part_index} narration is empty.")
        amount = word_count(part_text)

        if mode == "short" and not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
            raise RuntimeError(
                f"SHORT part {part_index} has {amount} words; required 120–150."
            )
        if int(part.get("word_count", -1)) != amount:
            raise RuntimeError(f"Part {part_index} word_count metadata mismatch.")

        part_scenes = part.get("scenes")
        if not isinstance(part_scenes, list) or len(part_scenes) != expected_scenes:
            raise RuntimeError(f"Part {part_index} scene count mismatch.")

        scene_narrations = []
        for scene_index, scene in enumerate(part_scenes, 1):
            global_index += 1
            expected_id = f"part_{part_index:02d}_scene_{scene_index:02d}"
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid scene: {expected_id}.")
            if scene.get("scene_id") != expected_id:
                raise RuntimeError(f"Scene ID mismatch: {expected_id}.")
            if int(scene.get("part", -1)) != part_index:
                raise RuntimeError(f"Scene part mismatch: {expected_id}.")
            if int(scene.get("scene", -1)) != scene_index:
                raise RuntimeError(f"Scene number mismatch: {expected_id}.")
            if int(scene.get("global_scene_number", -1)) != global_index:
                raise RuntimeError(f"Global scene number mismatch: {expected_id}.")

            for field in ("narration", "visual", "visual_prompt"):
                if not clean(scene.get(field)):
                    raise RuntimeError(f"Missing {field}: {expected_id}.")
            if clean(scene["visual"]) != clean(scene["visual_prompt"]):
                raise RuntimeError(f"Visual prompt mismatch: {expected_id}.")

            scene_narrations.append(clean(scene["narration"]))
            flattened.append(scene)

        if clean(" ".join(scene_narrations)) != part_text:
            raise RuntimeError(
                f"Part {part_index} narration differs from ordered scene narration."
            )
        complete_parts_text.append(part_text)

    if flattened != scenes:
        raise RuntimeError("Top-level scenes differ from part scene records.")

    combined = clean(" ".join(complete_parts_text))
    if clean(story.get("story_text")) != combined:
        raise RuntimeError("story_text does not match ordered part narrations.")

    checks = {
        "total_scenes": expected_total,
        "total_parts": expected_parts,
        "parts_count": expected_parts,
        "total_story_words": word_count(story.get("story_text", "")),
        "source_word_count": word_count(story.get("source_text", "")),
    }
    for key, expected in checks.items():
        try:
            actual = int(story.get(key, -1))
        except (TypeError, ValueError):
            actual = -1
        if actual != expected:
            raise RuntimeError(
                f"Story metadata mismatch for {key}: expected {expected}, got {actual}."
            )
    return True


def load_full_story_plan(
    mode: str,
    topic: str,
    parts_count: int,
    scenes_per_part: int,
) -> dict:
    if not PLAN_FILE.is_file():
        raise RuntimeError("Required story_plan.json is missing.")
    plan = read_json(PLAN_FILE)
    if not isinstance(plan, dict):
        raise RuntimeError("story_plan.json is invalid JSON.")
    validate_generated_plan(plan, mode, topic, parts_count, scenes_per_part)
    return plan


def build_short_story_plan(
    config: dict,
    topic: str,
    story_text: str,
    parts_count: int,
    scenes_per_part: int,
) -> dict:
    plan = build_story_plan(
        config=config,
        format_type="short",
        topic=topic,
        story_text=story_text,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
        dynamic_short=False,
    )
    validate_generated_plan(plan, "short", topic, parts_count, scenes_per_part)
    write_json(PLAN_FILE, plan)
    return plan


def cache_is_valid(
    cached: dict | None,
    fingerprint: str,
    mode: str,
    parts_count: int,
    scenes_per_part: int,
) -> bool:
    if not isinstance(cached, dict):
        return False
    if cached.get("source_fingerprint") != fingerprint:
        return False
    if cached.get("status") != "completed":
        return False
    try:
        validate_story(cached, parts_count, scenes_per_part, mode)
        return True
    except (RuntimeError, TypeError, ValueError, KeyError):
        return False


def main() -> None:
    STORY_DIR.mkdir(parents=True, exist_ok=True)
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Missing input configuration: {INPUT_FILE}")

    config = load_and_validate(str(INPUT_FILE))
    mode = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    title_input = clean(config.get("TITLE", ""))
    source_text = get_complete_source(config, INPUT_FILE)

    parts_specification = clean(
        config.get("PARTS_SPEC") or config.get("PARTS") or get_parts(config)
    )
    minimum_parts, maximum_parts = parse_parts_range(parts_specification)
    configured_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    story_length = config.get("STORY_LENGTH", "auto")

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}.")
    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")
    if not minimum_parts <= configured_parts <= maximum_parts:
        raise RuntimeError(
            f"Selected PARTS={configured_parts} is outside configured "
            f"range {minimum_parts}-{maximum_parts}."
        )
    if not topic and not title_input and not source_text:
        raise RuntimeError("Provide TOPIC, TITLE or STORY_TEXT in Input/topic.txt.")
    if source_text and word_count(source_text) < 20:
        raise RuntimeError("STORY_TEXT contains fewer than 20 words.")

    provisional_count = minimum_parts if mode == "short" else configured_parts
    api_key = gemini_api_key()
    model = selected_model() if api_key else ("source" if source_text else "offline")

    fingerprint = digest({
        "version": VERSION,
        "format": mode,
        "parts_specification": parts_specification,
        "configured_parts": configured_parts,
        "scenes": scenes_per_part,
        "story_length": story_length,
        "topic": topic,
        "title": title_input,
        "source_text": source_text,
        "model": model,
    })

    log("FORMAT:", mode)
    log("TITLE:", title_input or "(not supplied)")
    log("TOPIC:", topic or "(not supplied)")
    log("SUPPLIED STORY WORDS:", word_count(source_text))
    log("PARTS SPECIFICATION:", parts_specification)
    log("PROVISIONAL PARTS:", provisional_count)
    log("SCENES PER PART:", scenes_per_part)
    log("SELECTED MODEL:", model)

    cached = read_json(OUTPUT_FILE)
    if (
        isinstance(cached, dict)
        and cached.get("source_fingerprint") == fingerprint
        and cached.get("status") == "completed"
    ):
        cached_parts = cached.get("total_parts")
        if isinstance(cached_parts, int) and minimum_parts <= cached_parts <= maximum_parts:
            try:
                validate_story(cached, cached_parts, scenes_per_part, mode)
                if mode == "short":
                    build_short_story_plan(
                        config, topic, cached["story_text"],
                        cached_parts, scenes_per_part,
                    )
                else:
                    load_full_story_plan(
                        mode, topic, cached_parts, scenes_per_part,
                    )
                atomic_write(TITLE_FILE, cached["title"] + "\n")
                log("Matching completed story reused.")
                log("Story API calls: 0")
                log("STORY GENERATION: SUCCESS")
                return
            except (RuntimeError, TypeError, ValueError, KeyError):
                log("Cached story or matching plan is invalid; rebuilding.")

    narrative = generate_narrative(
        config=config,
        mode=mode,
        topic=topic,
        title=title_input,
        source_text=source_text,
        parts_count=provisional_count,
        model=model,
    )

    selected_parts, final_parts_count, complete_story = select_story_parts(
        narrative=narrative,
        mode=mode,
        parts_specification=parts_specification,
        story_length=story_length,
        config=config,
        provisional_count=provisional_count,
        model=model,
    )

    if len(selected_parts) != final_parts_count:
        raise RuntimeError("Final selected part count does not match generated parts.")
    if mode == "short":
        validate_short_parts(selected_parts)

    final_narrative = {
        "title": (
            title_input
            or clean(narrative.get("title"))
            or derive_title_from_story(complete_story)
        ),
        "parts": selected_parts,
        "story_text": "\n\n".join(selected_parts),
        "generation_method": narrative.get("generation_method", "gemini"),
        "model": narrative.get("model", model),
        "proofreading": narrative.get("proofreading", {
            "performed": False,
            "method": "not_recorded",
        }),
    }

    if mode == "short":
        plan = build_short_story_plan(
            config,
            topic,
            final_narrative["story_text"],
            final_parts_count,
            scenes_per_part,
        )
    else:
        plan = load_full_story_plan(
            mode, topic, final_parts_count, scenes_per_part
        )

    story = make_story(
        config=config,
        plan=plan,
        narrative=final_narrative,
        source_text=source_text,
        topic=topic,
        mode=mode,
        parts_count=final_parts_count,
        scenes_per_part=scenes_per_part,
    )

    story["source_fingerprint"] = fingerprint
    story["configured_parts_specification"] = parts_specification
    story["configured_parts_min"] = minimum_parts
    story["configured_parts_max"] = maximum_parts
    story["selected_parts_count"] = final_parts_count
    story["corrected_complete_story_word_count"] = word_count(complete_story)

    validate_story(story, final_parts_count, scenes_per_part, mode)
    write_json(OUTPUT_FILE, story)
    write_json(CACHE_FILE, {
        "version": VERSION,
        "source_fingerprint": fingerprint,
        "status": "completed",
        "title": story["title"],
        "format": mode,
        "total_parts": final_parts_count,
        "story": story,
    })
    atomic_write(TITLE_FILE, story["title"] + "\n")

    log("=" * 58)
    log("STORY GENERATION: SUCCESS")
    log("Version:", VERSION)
    log("Format:", mode)
    log("Title:", story["title"])
    log("Source words:", story["source_word_count"])
    log("Corrected complete story words:", word_count(complete_story))
    log("Generated words:", story["total_story_words"])
    log("Configured PARTS:", parts_specification)
    log("Selected PARTS:", story["total_parts"])
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", story["total_scenes"])
    log("Generation method:", story["generation_method"])
    log("Actual model:", story["model"])
    log("Output:", OUTPUT_FILE)
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("STORY GENERATION: CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log("STORY GENERATION: FAILED")
        log(type(exc).__name__ + ":", str(exc))
        raise