#!/usr/bin/env python3
"""Katha Lok AI — source-preserving Hindi story generation."""

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

VERSION = "story-builder-v27-complete-source-and-part-policy"

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

SENTENCE_ENDINGS = ("।", ".", "!", "?", "！？")
CLAUSE_ENDINGS = (",", "，", ";", ":", "—", "–")

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
    text = str(value or "")
    text = text.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def word_count(value: Any) -> int:
    return len(re.findall(r"\S+", clean(str(value or ""))))


def digest(data: Any) -> str:
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
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
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def read_json(path: str | Path) -> dict | None:
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8-sig")
        )
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def read_story_text_direct(path: str | Path) -> str:
    """Read the complete multiline STORY_TEXT assignment."""
    file_path = Path(path)

    if not file_path.is_file():
        raise FileNotFoundError(str(file_path))

    lines = file_path.read_text(
        encoding="utf-8-sig"
    ).splitlines()

    for index, line in enumerate(lines):
        match = re.match(
            r"^\s*STORY_TEXT\s*=\s*(.*?)\s*$",
            line,
        )
        if not match:
            continue

        value = match.group(1).strip()
        delimiter = None

        if value.startswith('"""'):
            delimiter = '"""'
        elif value.startswith("'''"):
            delimiter = "'''"

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

            raise RuntimeError(
                "STORY_TEXT multiline block has no closing delimiter."
            )

        if len(value) >= 2 and value[0] == value[-1]:
            if value[0] in {"'", '"'}:
                return value[1:-1]

        return value

    return ""


def get_complete_source(config: dict, input_path: str | Path) -> str:
    parsed = get_story_text(config)
    direct = read_story_text_direct(input_path)

    if word_count(direct) > word_count(parsed):
        log(
            "STORY_TEXT parser mismatch; using complete direct input:",
            word_count(direct),
            "words",
        )
        return normalize_story_text(direct)

    return normalize_story_text(parsed or direct)


def split_sentences(text: str) -> list[str]:
    """Split without discarding sentence-ending punctuation."""
    result = []

    for block in re.split(r"\n+", normalize_story_text(text)):
        block = block.strip()
        if not block:
            continue

        pieces = re.split(r"(?<=[।.!?！？])\s+", block)

        for piece in pieces:
            piece = clean(piece)
            if piece:
                result.append(piece)

    return result


def split_clauses(sentence: str) -> list[str]:
    sentence = clean(sentence)
    if not sentence:
        return []

    return [
        clean(piece)
        for piece in re.split(
            r"(?<=[,，;:—–])\s+",
            sentence,
        )
        if clean(piece)
    ]


def split_balanced(text: str, count: int) -> list[str]:
    """
    Split into exactly count groups.
    Prefer complete sentences, then clauses, and only then word boundaries.
    Preserve every word in its original order.
    """
    if count < 1:
        raise RuntimeError("Group count must be positive.")

    sentences = split_sentences(text)

    if not sentences:
        raise RuntimeError("Story has no usable sentences.")

    units = sentences[:]

    if len(units) < count:
        units = []
        for sentence in sentences:
            units.extend(split_clauses(sentence))

    if len(units) < count:
        tokens = re.findall(r"\S+", " ".join(units))

        if len(tokens) < count:
            raise RuntimeError(
                "Not enough words to create the requested groups."
            )

        units = [
            " ".join(
                tokens[
                    round(i * len(tokens) / count):
                    round((i + 1) * len(tokens) / count)
                ]
            )
            for i in range(count)
        ]

        if any(not clean(item) for item in units):
            raise RuntimeError("Could not create non-empty groups.")

        return units

    groups = []
    cursor = 0

    for index in range(count):
        remaining_groups = count - index

        if remaining_groups == 1:
            groups.append(" ".join(units[cursor:]))
            break

        remaining_units = len(units) - cursor
        maximum = remaining_units - remaining_groups + 1

        remaining_words = sum(
            word_count(item) for item in units[cursor:]
        )
        target = remaining_words / remaining_groups

        group = []
        amount = 0

        while cursor < len(units) and len(group) < maximum:
            candidate = units[cursor]
            candidate_words = word_count(candidate)

            if group:
                current_error = abs(amount - target)
                next_error = abs(
                    amount + candidate_words - target
                )
                if current_error <= next_error:
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
        raise RuntimeError(
            f"Could not create exactly {count} non-empty groups."
        )

    if word_count(" ".join(units)) != sum(
        word_count(group) for group in groups
    ):
        raise RuntimeError("Segmentation changed the word count.")

    return groups


def derive_title_from_story(text: str) -> str:
    """Create a fallback title using themes across the complete story."""
    sentences = split_sentences(text)

    if not sentences:
        return "हिंदी कहानी"

    weights = {}
    first_seen = {}
    total = len(sentences)

    for index, sentence in enumerate(sentences):
        tokens = set(
            re.sub(r"[^\w\s]", " ", sentence.lower()).split()
        )
        position = index / max(total - 1, 1)
        weight = (
            1.5 if position >= 0.75
            else 1.2 if position >= 0.20
            else 1.0
        )

        for token in tokens:
            if (
                len(token) <= 2
                or token in STOP_WORDS
                or token.isdigit()
            ):
                continue

            weights[token] = weights.get(token, 0.0) + weight
            first_seen.setdefault(token, index)

    if not weights:
        return "हिंदी कहानी"

    ranked = sorted(
        weights,
        key=lambda token: (
            -weights[token],
            first_seen[token],
            token,
        ),
    )

    title = " ".join(ranked[:5]).strip()
    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0]

    return title or "हिंदी कहानी"


def selected_model() -> str:
    data = read_json(MODEL_FILE)
    model = clean(data.get("model")) if data else ""

    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(
            "Selected Gemini model is missing or invalid."
        )

    return model


def gemini_api_key() -> str:
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )


class GeminiRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def extract_json_object(text: str) -> dict | None:
    """Accept direct JSON, fenced JSON, or JSON embedded in plain text."""
    value = str(text or "").strip()

    if not value:
        return None

    value = re.sub(
        r"^\s*```(?:json)?\s*|\s*```\s*$",
        "",
        value,
        flags=re.IGNORECASE,
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
    """
    Accept structured JSON and plain-text model responses.
    Plain text is treated as complete narration, not discarded.
    """
    parsed = extract_json_object(response_text)

    if parsed is not None:
        return parsed

    plain = normalize_story_text(response_text)
    plain = re.sub(
        r"^\s*(?:कहानी|शीर्षक|भाग\s*\d+)\s*[:：-]\s*",
        "",
        plain,
        flags=re.IGNORECASE,
    )

    if not plain:
        raise GeminiRequestError(
            "Gemini returned neither usable JSON nor plain story text."
        )

    return {
        "title": "",
        "story_text": plain,
        "parts": [],
        "response_format": "plain_text",
    }


def call_gemini_once(
    prompt: str,
    model: str,
    api_key: str,
) -> dict:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model, safe="-._")
        + ":generateContent"
    )

    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.30,
            "maxOutputTokens": MAX_RESPONSE_TOKENS,
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
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )
        raise GeminiRequestError(
            f"Gemini HTTP {exc.code}: {body[:1800]}",
            status=exc.code,
            retryable=exc.code in RETRYABLE_HTTP,
        ) from exc

    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GeminiRequestError(
            f"Gemini network error: {exc}",
            retryable=True,
        ) from exc

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GeminiRequestError(
            "Gemini API returned invalid response JSON."
        ) from exc

    candidates = result.get("candidates", [])
    if not candidates:
        raise GeminiRequestError(
            "Gemini returned no candidate: "
            + json.dumps(
                result.get("promptFeedback", {}),
                ensure_ascii=False,
            )
        )

    response_parts = (
        candidates[0].get("content", {}).get("parts", [])
    )
    response_text = "".join(
        part.get("text", "")
        for part in response_parts
        if isinstance(part, dict)
    ).strip()

    if not response_text:
        raise GeminiRequestError(
            "Gemini returned an empty response."
        )

    return parse_model_story_response(response_text)


def call_gemini(
    prompt: str,
    model: str,
    api_key: str,
) -> dict:
    """Retry only the model already selected in the project config."""
    errors = []

    for attempt in range(MAX_RETRIES):
        try:
            log(
                f"Gemini model={model}; "
                f"attempt={attempt + 1}/{MAX_RETRIES}"
            )
            result = call_gemini_once(prompt, model, api_key)
            log("Gemini story request succeeded:", model)
            return result

        except GeminiRequestError as exc:
            errors.append(str(exc))
            log("Gemini request failed:", str(exc))

            if not exc.retryable:
                break

            if attempt + 1 < MAX_RETRIES:
                delay = RETRY_DELAYS[
                    min(attempt, len(RETRY_DELAYS) - 1)
                ]
                time.sleep(delay)

    raise RuntimeError(
        f"Selected Gemini model {model} failed: "
        + " | ".join(errors)
    )


def build_generation_prompt(
    mode: str,
    topic: str,
    title: str,
    source_text: str,
    parts_count: int,
    config: dict,
) -> str:
    if mode == "short":
        format_rules = f"""
SHORT RULES:
- Return exactly {parts_count} parts.
- Aim for 120–150 words in every part.
- Never exceed 180 words in any part.
- If a part is above 150 words, revise it before returning.
- Keep every part coherent and in chronological order.
- Preserve all important events, characters and the original ending.
- Each part opening must use a distinct story-specific hook.
- Each non-final part must end at a genuine suspense point.
- Do not invent a new twist, event, character or ending.
"""
    else:
        format_rules = f"""
FULL RULES:
- Return exactly {parts_count} parts.
- Preserve the complete supplied story and its original ending.
- Use a longer story-specific opening hook.
- Build suspense naturally through the complete story.
- Do not create an additional SHORT version.
"""

    source_instruction = (
        "Rewrite the supplied complete story while preserving its plot."
        if source_text
        else "Create a complete original Hindi story from the supplied topic."
    )

    return f"""
You are a professional Hindi cinematic storyteller.

FORMAT: {mode}
EXACT PART COUNT: {parts_count}
AUDIENCE: {clean(str(config.get("AUDIENCE", "adult")))}
EXPLICIT TITLE/TOPIC: {title or topic or "(not supplied)"}

TASK:
{source_instruction}

COMPLETE SOURCE STORY:
<source_story>
{source_text}
</source_story>

TOPIC:
{topic or "(not supplied)"}

MANDATORY STORY RULES:
- Read the complete source from beginning to ending before writing.
- Understand the complete plot before generating the narration.
- Preserve characters, relationships, motivations and outcomes.
- Preserve locations, objects, clues, events and consequences.
- Preserve chronology and the original ending.
- Correct Hindi matras, spelling, grammar and punctuation.
- Use natural Hindi Devanagari unless the source requires otherwise.
- Improve narration without changing the underlying story.
- Do not repeat information merely to increase word count.
- Do not add camera directions or production notes.

TITLE RULE:
- Use an explicitly supplied TITLE when present.
- If TITLE is blank and a complete story is supplied, read the entire story
  and create a title from its central conflict, important events and outcome.
- Never use only the first line or opening sentence to create the title.
- If only TOPIC is supplied, create a relevant story title.

HOOK AND SUSPENSE:
- The opening hook must relate to an actual event in this story.
- The FULL opening hook may be longer than the SHORT opening hook.
- Build suspense from real clues, danger and consequences.
- Avoid generic repeated suspense phrases.
- Do not fabricate a twist, clue or ending.
- The final part must deliver the story's actual resolution.

SEGMENTATION:
- Prefer complete sentences and natural paragraph boundaries.
- Prefer पूर्ण विराम, question marks and exclamation marks.
- Use clause boundaries only where a sentence is too long.
- Do not break a sentence merely to equalize word counts.
- Keep every part coherent and in chronological order.

{format_rules}

Return valid JSON only:
{{
  "title": "story title based on the complete story",
  "story_text": "complete corrected story",
  "parts": [
    {{"part": 1, "text": "complete narration"}}
  ]
}}

If returning parts, every part number must be sequential.
Do not include markdown fences or explanation outside the JSON.
"""


def extract_part_text(row: Any, number: int) -> str:
    if isinstance(row, str):
        text = clean(row)
        if text:
            return text

    if not isinstance(row, dict):
        raise RuntimeError(
            f"Invalid generated story part {number}."
        )

    try:
        returned_number = int(row.get("part", number))
    except (TypeError, ValueError):
        returned_number = -1

    if returned_number != number:
        raise RuntimeError(
            f"Generated part numbering mismatch at part {number}."
        )

    text = clean(
        row.get("text")
        or row.get("story")
        or row.get("narration")
        or row.get("content")
    )

    if not text:
        raise RuntimeError(
            f"Generated story part {number} is empty."
        )

    return text


def extract_response_parts(data: dict) -> list[str]:
    rows = data.get("parts")

    if isinstance(rows, list) and rows:
        return [
            extract_part_text(row, number)
            for number, row in enumerate(rows, 1)
        ]

    complete_text = clean(
        data.get("story_text")
        or data.get("story")
        or data.get("narration")
        or data.get("text")
    )

    if complete_text:
        return [complete_text]

    raise RuntimeError(
        "Gemini response contains neither story parts nor complete story text."
    )


def validate_short_parts(parts: list[Any]) -> bool:
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("SHORT story has no parts.")

    errors = []

    for number, item in enumerate(parts, 1):
        text = (
            clean(
                item.get("text")
                or item.get("story")
                or item.get("narration")
            )
            if isinstance(item, dict)
            else clean(str(item))
        )
        count = word_count(text)

        if count < SHORT_MIN_WORDS:
            errors.append(
                f"Part {number}: {count} words, below 120."
            )
        elif count > SHORT_HARD_MAX_WORDS:
            errors.append(
                f"Part {number}: {count} words, above hard limit 180."
            )

    if errors:
        raise RuntimeError(
            "SHORT word-count validation failed: "
            + " | ".join(errors)
        )

    return True


def repair_short_parts(
    parts: list[Any],
    source_text: str,
    topic: str,
    title: str,
    model: str,
    api_key: str,
) -> list[str]:
    """Revise only invalid or over-150-word parts; preserve valid parts."""
    if not isinstance(parts, list):
        raise RuntimeError("SHORT parts must be a list.")

    repaired = []

    for index, original in enumerate(parts):
        original_text = (
            clean(
                original.get("text")
                or original.get("story")
                or original.get("narration")
            )
            if isinstance(original, dict)
            else clean(str(original))
        )
        count = word_count(original_text)

        if SHORT_MIN_WORDS <= count <= SHORT_MAX_WORDS:
            repaired.append(original_text)
            log(f"SHORT part {index + 1}: valid ({count} words)")
            continue

        if count > SHORT_HARD_MAX_WORDS:
            target_instruction = (
                "Reduce the narration to 120–150 words without removing "
                "essential story events. It must never exceed 180 words."
            )
        elif count < SHORT_MIN_WORDS:
            target_instruction = (
                "Expand the narration to 120–150 words by restoring "
                "relevant details already present in the complete story. "
                "Do not invent events."
            )
        else:
            target_instruction = (
                "Revise the narration to 120–150 words while preserving "
                "all essential events and complete sentences."
            )

        corrected = None
        errors = []

        for attempt in range(SHORT_REPAIR_ATTEMPTS):
            prompt = f"""
Revise one part of a Hindi story.

PART NUMBER: {index + 1}
CURRENT WORD COUNT: {count}
REQUIRED TARGET: 120–150 words
HARD MAXIMUM: 180 words

TITLE: {title or topic or "कहानी"}

COMPLETE ORIGINAL STORY:
{source_text or "(Use the full generated story context.)"}

CURRENT PART:
{original_text}

INSTRUCTIONS:
- {target_instruction}
- Preserve characters, relationships, events and chronology.
- Do not invent a new event, character, clue, twist or ending.
- Preserve continuity with the neighbouring parts.
- Correct matras, spelling, grammar and punctuation.
- Keep complete sentences together.
- Return only the revised narration.

Return JSON:
{{"text": "revised narration"}}
"""

            try:
                result = call_gemini(prompt, model, api_key)
                candidate = clean(
                    result.get("text")
                    or result.get("story")
                    or result.get("narration")
                )

                if not candidate and result.get("story_text"):
                    candidate = clean(result["story_text"])

                candidate_count = word_count(candidate)

                if (
                    candidate
                    and SHORT_MIN_WORDS <= candidate_count
                    <= SHORT_MAX_WORDS
                ):
                    corrected = candidate
                    break

                errors.append(
                    f"Attempt {attempt + 1}: "
                    f"{candidate_count} words"
                )

            except Exception as exc:
                errors.append(
                    f"Attempt {attempt + 1}: {exc}"
                )

        if corrected is None:
            raise RuntimeError(
                f"SHORT part {index + 1} revision failed: "
                + " | ".join(errors)
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
                "Topic/title-only story generation requires "
                "GEMINI_API_KEY or GOOGLE_API_KEY."
            )

        if mode == "short":
            parts = split_balanced(source_text, parts_count)
            parts = repair_offline_short_parts(parts)
        else:
            parts = split_balanced(source_text, parts_count)

        return {
            "title": title or derive_title_from_story(source_text),
            "parts": parts,
            "story_text": "\n\n".join(parts),
            "generation_method": "offline_supplied_story",
            "model": "offline",
        }

    prompt = build_generation_prompt(
        mode=mode,
        topic=topic,
        title=title,
        source_text=source_text,
        parts_count=parts_count,
        config=config,
    )

    data = call_gemini(prompt, model, api_key)
    parts = extract_response_parts(data)

    complete_story = clean(
        data.get("story_text")
        or data.get("story")
        or data.get("narration")
        or data.get("text")
    )

    if not complete_story:
        complete_story = "\n\n".join(parts)

    if not complete_story:
        raise RuntimeError("Generated complete story is empty.")

    returned_title = clean(data.get("title"))
    final_title = title or returned_title

    if not final_title:
        final_title = derive_title_from_story(complete_story)

    return {
        "title": final_title[:180],
        "parts": parts,
        "story_text": complete_story,
        "generation_method": "gemini",
        "model": model,
    }


def repair_offline_short_parts(parts: list[str]) -> list[str]:
    """Offline mapping cannot rewrite; validate rather than silently alter."""
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("SHORT story has no parts.")

    for index, part in enumerate(parts, 1):
        count = word_count(part)
        if not SHORT_MIN_WORDS <= count <= SHORT_HARD_MAX_WORDS:
            raise RuntimeError(
                f"Offline SHORT part {index} has {count} words. "
                f"Required range: 120–180. "
                "A configured story model is required to revise it."
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
    """
    Select the final part count only after the complete corrected story
    has been assembled and counted.
    """
    raw_parts = narrative.get("parts")
    complete_story = clean(
        narrative.get("story_text")
        or narrative.get("story")
        or narrative.get("narration")
    )

    if not complete_story and isinstance(raw_parts, list):
        complete_story = "\n\n".join(
            extract_part_text(row, index)
            if isinstance(row, dict)
            else clean(str(row))
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

        log(
            "Corrected complete story words:",
            word_count(complete_story),
        )
        log("Configured PARTS:", parts_specification)
        log("Final selected SHORT parts:", selected_count)

        # Regroup the complete story after selecting the final count.
        selected_parts = split_balanced(
            complete_story,
            selected_count,
        )

        # Repair only parts outside the preferred 120–150 range.
        api_key = gemini_api_key()

        if api_key:
            selected_parts = repair_short_parts(
                parts=selected_parts,
                source_text=complete_story,
                topic=clean(get_topic(config)),
                title=clean(narrative.get("title")),
                model=model,
                api_key=api_key,
            )
        else:
            selected_parts = repair_offline_short_parts(
                selected_parts
            )

        validate_short_parts(selected_parts)

        # The part count remains fixed after selection.
        if len(selected_parts) != selected_count:
            raise RuntimeError(
                "Final SHORT part count changed during revision."
            )

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
            f"the configured range {minimum}-{maximum}."
        )

    if isinstance(raw_parts, list) and len(raw_parts) == selected_count:
        selected_parts = [
            extract_part_text(row, index)
            if isinstance(row, dict)
            else clean(str(row))
            for index, row in enumerate(raw_parts, 1)
        ]
    else:
        selected_parts = split_balanced(
            complete_story,
            selected_count,
        )

    if len(selected_parts) != selected_count:
        raise RuntimeError(
            "FULL story part count does not match the configured count."
        )

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
    title = clean(narrative.get("title"))

    if not title:
        title = derive_title_from_story(
            narrative.get("story_text")
            or source_text
            or "\n\n".join(
                str(item) for item in narrative.get("parts", [])
            )
        )

    raw_parts = narrative.get("parts")

    if (
        not isinstance(raw_parts, list)
        or len(raw_parts) != parts_count
    ):
        raise RuntimeError(
            f"Story part mismatch: expected {parts_count}, "
            f"received {len(raw_parts) if isinstance(raw_parts, list) else 0}."
        )

    plan_parts = plan.get("parts")

    if (
        not isinstance(plan_parts, list)
        or len(plan_parts) != parts_count
    ):
        raise RuntimeError(
            "Story plan part count does not match the final story."
        )

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
            raise RuntimeError(
                f"Part {part_index} has empty narration."
            )

        amount = word_count(part_text)

        if mode == "short" and not (
            SHORT_MIN_WORDS <= amount <= SHORT_HARD_MAX_WORDS
        ):
            raise RuntimeError(
                f"SHORT part {part_index} has {amount} words; "
                "required 120–180."
            )

        if mode == "short" and amount > SHORT_MAX_WORDS:
            raise RuntimeError(
                f"SHORT part {part_index} still has {amount} words "
                "after revision. It must be revised to 150 or fewer."
            )

        scene_texts = split_balanced(
            part_text,
            scenes_per_part,
        )

        if len(scene_texts) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_index} scene count mismatch."
            )

        if word_count(" ".join(scene_texts)) != amount:
            raise RuntimeError(
                f"Scene splitting changed narration in part {part_index}."
            )

        plan_part = plan_parts[part_index - 1]
        plan_scenes = (
            plan_part.get("scenes")
            if isinstance(plan_part, dict)
            else None
        )

        if (
            not isinstance(plan_scenes, list)
            or len(plan_scenes) != scenes_per_part
        ):
            raise RuntimeError(
                f"Story plan scenes are invalid for part {part_index}."
            )

        part_scenes = []

        for scene_index, narration_text in enumerate(
            scene_texts,
            1,
        ):
            global_scene += 1
            plan_scene = plan_scenes[scene_index - 1]

            if not isinstance(plan_scene, dict):
                raise RuntimeError(
                    f"Invalid plan scene in part {part_index}, "
                    f"scene {scene_index}."
                )

            scene_id = (
                f"part_{part_index:02d}_scene_{scene_index:02d}"
            )

            anchor = clean(plan_scene.get("story_anchor"))
            purpose = clean(plan_scene.get("purpose"))

            visual = clean(
                plan_scene.get("visual")
                or plan_scene.get("visual_prompt")
                or plan_scene.get("description")
            )

            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene. "
                    "Show the exact story moment described by this "
                    f"narration: {narration_text}. "
                    f"Story context: {anchor or topic or title}. "
                    "Include the people, objects, location and actions "
                    "actually described in the narration. "
                    "Maintain consistent character identity, face, "
                    "age, clothing, location, objects and chronology. "
                    "Use realistic human anatomy, natural expressions, "
                    "credible lighting and cinematic camera framing. "
                    "Do not use cartoon, illustration or unrelated imagery."
                )

            scene_row = {
                "part": part_index,
                "scene": scene_index,
                "scene_id": scene_id,
                "global_scene_number": global_scene,
                "purpose": purpose,
                "role": clean(plan_scene.get("role")),
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

    story_text = "\n\n".join(
        part["narration"] for part in story_parts
    )

    result = {
        "version": VERSION,
        "status": "completed",
        "title": title,
        "final_title": title,
        "topic": topic,
        "format": mode,
        "model": clean(narrative.get("model")) or "offline",
        "generation_method": narrative.get(
            "generation_method",
            "offline_or_gemini",
        ),
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

    validate_story(
        result,
        parts_count,
        scenes_per_part,
        mode,
    )

    return result


def validate_story(
    story: dict,
    expected_parts: int,
    expected_scenes: int,
    mode: str,
) -> bool:
    if not isinstance(story, dict):
        raise RuntimeError("Story output must be a JSON object.")

    if story.get("status") != "completed":
        raise RuntimeError("Story status is not completed.")

    if not clean(story.get("title")):
        raise RuntimeError("Story title is missing.")

    if clean(story.get("format")).lower() != mode:
        raise RuntimeError("Story FORMAT does not match input.")

    parts = story.get("parts")
    scenes = story.get("scenes")

    if not isinstance(parts, list) or len(parts) != expected_parts:
        raise RuntimeError(
            f"Story part count mismatch: expected {expected_parts}."
        )

    expected_total = expected_parts * expected_scenes

    if not isinstance(scenes, list) or len(scenes) != expected_total:
        raise RuntimeError(
            f"Story scene count mismatch: expected {expected_total}."
        )

    flattened = []
    complete_parts_text = []
    global_index = 0

    for part_index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part {part_index}.")

        if int(part.get("part", -1)) != part_index:
            raise RuntimeError(
                f"Story part numbering mismatch: {part_index}."
            )

        part_text = clean(part.get("narration"))
        if not part_text:
            raise RuntimeError(
                f"Part {part_index} narration is empty."
            )

        amount = word_count(part_text)

        if mode == "short":
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"SHORT part {part_index} has {amount} words; "
                    "required 120–150."
                )

        if int(part.get("word_count", -1)) != amount:
            raise RuntimeError(
                f"Part {part_index} word_count metadata mismatch."
            )

        part_scenes = part.get("scenes")
        if (
            not isinstance(part_scenes, list)
            or len(part_scenes) != expected_scenes
        ):
            raise RuntimeError(
                f"Part {part_index} scene count mismatch."
            )

        scene_narrations = []

        for scene_index, scene in enumerate(part_scenes, 1):
            global_index += 1
            expected_id = (
                f"part_{part_index:02d}_scene_{scene_index:02d}"
            )

            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid scene: {expected_id}.")

            if scene.get("scene_id") != expected_id:
                raise RuntimeError(f"Scene ID mismatch: {expected_id}.")

            if int(scene.get("part", -1)) != part_index:
                raise RuntimeError(f"Scene part mismatch: {expected_id}.")

            if int(scene.get("scene", -1)) != scene_index:
                raise RuntimeError(f"Scene number mismatch: {expected_id}.")

            if int(scene.get("global_scene_number", -1)) != global_index:
                raise RuntimeError(
                    f"Global scene number mismatch: {expected_id}."
                )

            for field in ("narration", "visual", "visual_prompt"):
                if not clean(scene.get(field)):
                    raise RuntimeError(
                        f"Missing {field}: {expected_id}."
                    )

            if clean(scene["visual"]) != clean(scene["visual_prompt"]):
                raise RuntimeError(
                    f"Visual prompt mismatch: {expected_id}."
                )

            scene_narrations.append(clean(scene["narration"]))
            flattened.append(scene)

        if clean(" ".join(scene_narrations)) != part_text:
            raise RuntimeError(
                f"Part {part_index} narration differs from "
                "its ordered scene narration."
            )

        complete_parts_text.append(part_text)

    if flattened != scenes:
        raise RuntimeError(
            "Top-level scenes list differs from part scene records."
        )

    combined_story = clean(" ".join(complete_parts_text))

    if clean(story.get("story_text")) != combined_story:
        raise RuntimeError(
            "story_text does not match ordered part narrations."
        )

    metadata_checks = {
        "total_scenes": expected_total,
        "total_parts": expected_parts,
        "parts_count": expected_parts,
        "total_story_words": word_count(story.get("story_text", "")),
        "source_word_count": word_count(story.get("source_text", "")),
    }

    for key, expected in metadata_checks.items():
        try:
            actual = int(story.get(key, -1))
        except (TypeError, ValueError):
            actual = -1

        if actual != expected:
            raise RuntimeError(
                f"Story metadata mismatch for {key}: "
                f"expected {expected}, got {actual}."
            )

    return True


def load_full_story_plan(
    mode: str,
    topic: str,
    parts_count: int,
    scenes_per_part: int,
) -> dict:
    if not PLAN_FILE.is_file():
        raise RuntimeError(
            "Required story_plan.json is missing. "
            "Generate story plan step must run first."
        )

    plan = read_json(PLAN_FILE)

    if not isinstance(plan, dict):
        raise RuntimeError("story_plan.json is invalid JSON.")

    validate_generated_plan(
        plan,
        mode,
        topic,
        parts_count,
        scenes_per_part,
    )

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

    validate_generated_plan(
        plan,
        "short",
        topic,
        parts_count,
        scenes_per_part,
    )

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
        validate_story(
            cached,
            parts_count,
            scenes_per_part,
            mode,
        )
        return True
    except (RuntimeError, TypeError, ValueError, KeyError):
        return False


def main() -> None:
    STORY_DIR.mkdir(parents=True, exist_ok=True)

    if not INPUT_FILE.is_file():
        raise RuntimeError(
            f"Missing input configuration: {INPUT_FILE}"
        )

    config = load_and_validate(str(INPUT_FILE))

    mode = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    title_input = clean(config.get("TITLE", ""))

    source_text = get_complete_source(config, INPUT_FILE)

    parts_specification = clean(
        config.get("PARTS_SPEC")
        or config.get("PARTS")
        or get_parts(config)
    )

    minimum_parts, maximum_parts = parse_parts_range(
        parts_specification
    )

    configured_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))
    story_length = config.get("STORY_LENGTH", "auto")

    if mode not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT: {mode}."
        )

    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError(
            "PARTS and SCENES must be positive integers."
        )

    if not minimum_parts <= configured_parts <= maximum_parts:
        raise RuntimeError(
            f"Selected PARTS={configured_parts} is outside "
            f"configured range {minimum_parts}-{maximum_parts}."
        )

    if not topic and not title_input and not source_text:
        raise RuntimeError(
            "Provide TOPIC, TITLE or STORY_TEXT in Input/topic.txt."
        )

    if source_text and word_count(source_text) < 20:
        raise RuntimeError(
            "STORY_TEXT contains fewer than 20 words. "
            "Check the complete multiline story."
        )

    # For a range, generate an initial complete story using the
    # configured lower bound. The final SHORT count is selected only
    # after the complete corrected story has been counted.
    provisional_count = (
        minimum_parts
        if mode == "short"
        else configured_parts
    )

    api_key = gemini_api_key()

    if api_key:
        model = selected_model()
    else:
        model = "source" if source_text else "offline"

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

    # A cached result is reusable only when its complete input fingerprint
    # and final output validation both match.
    if (
        isinstance(cached, dict)
        and cached.get("source_fingerprint") == fingerprint
        and cached.get("status") == "completed"
    ):
        cached_parts = cached.get("total_parts")

        if (
            isinstance(cached_parts, int)
            and minimum_parts <= cached_parts <= maximum_parts
        ):
            try:
                validate_story(
                    cached,
                    cached_parts,
                    scenes_per_part,
                    mode,
                )

                if mode == "short":
                    build_short_story_plan(
                        config,
                        topic,
                        cached["story_text"],
                        cached_parts,
                        scenes_per_part,
                    )
                else:
                    load_full_story_plan(
                        mode,
                        topic,
                        cached_parts,
                        scenes_per_part,
                    )

                atomic_write(
                    TITLE_FILE,
                    cached["title"] + "\n",
                )

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

    selected_parts, final_parts_count, complete_story = (
        select_story_parts(
            narrative=narrative,
            mode=mode,
            parts_specification=parts_specification,
            story_length=story_length,
            config=config,
            provisional_count=provisional_count,
            model=model,
        )
    )

    if len(selected_parts) != final_parts_count:
        raise RuntimeError(
            "Final selected part count does not match generated parts."
        )

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
        "generation_method": narrative.get(
            "generation_method",
            "offline_or_gemini",
        ),
        "model": narrative.get("model", model),
    }

    # The selected final part count is passed to the plan and scene mapping.
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
            mode,
            topic,
            final_parts_count,
            scenes_per_part,
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
    story["corrected_complete_story_word_count"] = word_count(
        complete_story
    )

    validate_story(
        story,
        final_parts_count,
        scenes_per_part,
        mode,
    )

    write_json(OUTPUT_FILE, story)

    write_json(
        CACHE_FILE,
        {
            "version": VERSION,
            "source_fingerprint": fingerprint,
            "status": "completed",
            "title": story["title"],
            "format": mode,
            "total_parts": final_parts_count,
            "story": story,
        },
    )

    atomic_write(
        TITLE_FILE,
        story["title"] + "\n",
    )

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