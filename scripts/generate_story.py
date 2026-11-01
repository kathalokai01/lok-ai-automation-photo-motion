#!/usr/bin/env python3
"""Katha Lok AI — source-preserving story generation and scene mapping."""

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
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_story_plan import (
    build_plan as build_story_plan,
    validate_plan as validate_generated_plan,
    calculate_dynamic_short_parts,
)

from input_config import (
    load_and_validate,
    get_format,
    get_topic,
    get_story_text,
    get_parts,
    get_scenes,
)

STORY_DIR = ROOT / "output" / "story"
PLAN_CANDIDATES = (
    STORY_DIR / "story_plan.json",
    ROOT / "output" / "config" / "story_plan.json",
)

OUTPUT_FILE = STORY_DIR / "story.json"
CACHE_FILE = STORY_DIR / "story_cache.json"
TITLE_FILE = STORY_DIR / "final_title.txt"

VERSION = "story-builder-v23-source-preserving"

SHORT_MIN_WORDS = 120
SHORT_TARGET_WORDS = 135
SHORT_MAX_WORDS = 150
SHORT_HARD_MAX_WORDS = 180

MAX_RESPONSE_TOKENS = 16000
REQUEST_TIMEOUT = 120
MAX_RETRIES_PER_MODEL = 2
RETRY_DELAYS = (5, 12)
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


def log(*values, file=None):
    print(*values, file=file or sys.stdout, flush=True)


def clean(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def word_count(text):
    return len(re.findall(r"\S+", clean(text)))


def digest(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def atomic_write(path, content):
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


def write_json(path, data):
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def read_json(path):
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8-sig")
        )
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def split_sentences(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    result = []

    for block in re.split(r"\n+", text):
        block = block.strip()
        if not block:
            continue

        for item in re.split(r"(?<=[।.!?！？])\s+", block):
            item = clean(item)
            if item:
                result.append(item)

    return result


def read_story_text_direct(path):
    """
    Read STORY_TEXT independently from the central config parser.
    This prevents a parser mismatch from silently reducing a long
    supplied story to only its first line or first paragraph.
    """
    file_path = Path(path)

    if not file_path.is_file():
        raise FileNotFoundError(f"Input config not found: {file_path}")

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

            for next_index in range(index + 1, len(lines)):
                current = lines[next_index]
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
            if value[0] in ("'", '"'):
                return value[1:-1]

        return value

    return ""


def get_complete_source(config, input_path):
    """
    Prefer the complete direct STORY_TEXT block when the config
    parser has returned a shorter or empty value.
    """
    parsed = get_story_text(config)
    direct = read_story_text_direct(input_path)

    parsed_words = word_count(parsed)
    direct_words = word_count(direct)

    if direct_words > parsed_words:
        log(
            "STORY_TEXT parser mismatch detected:",
            f"parsed={parsed_words}",
            f"direct={direct_words}",
        )
        log("Using complete STORY_TEXT read from Input/topic.txt.")
        return direct

    if parsed_words and direct_words and parsed_words != direct_words:
        log(
            "STORY_TEXT word-count difference:",
            f"parsed={parsed_words}",
            f"direct={direct_words}",
        )

    return parsed or direct


def derive_title_from_story(text):
    """Derive a title using the complete plot, not only its opening."""
    units = split_sentences(text)

    if not units:
        return "हिंदी कहानी"

    stop = {
        "और", "या", "लेकिन", "मगर", "क्योंकि", "इसलिए", "फिर",
        "तब", "जब", "जहाँ", "वहाँ", "जिस", "जिसने", "जिसका",
        "उसने", "उसका", "उसकी", "उसके", "उनके", "उनकी",
        "यह", "वह", "ये", "वे", "इस", "उस", "एक", "कोई",
        "कुछ", "सभी", "बहुत", "काफी", "था", "थी", "थे",
        "है", "हैं", "हो", "हुआ", "हुई", "हुए", "कर",
        "करके", "किया", "किए", "की", "का", "के", "में",
        "पर", "से", "तक", "लिए", "साथ", "अपने", "अपनी",
        "अपना", "नहीं", "नही", "भी", "ही", "तो", "अब",
        "आज", "कल", "वहां", "यहां", "रहा", "रही", "रहे",
        "बाद", "पहले", "सामने", "दौरान", "तरह", "बात",
        "कहानी", "कथा", "औरत", "आदमी",
    }

    weights = {}
    first_seen = {}
    total = len(units)

    for index, sentence in enumerate(units):
        normalized = re.sub(
            r"[^\w\s]", " ", sentence.lower()
        )

        tokens = {
            token for token in normalized.split()
            if len(token) > 2
            and token not in stop
            and not token.isdigit()
        }

        position = index / max(total - 1, 1)

        weight = (
            1.45 if position >= 0.75
            else 1.20 if position >= 0.20
            else 1.0
        )

        for token in tokens:
            weights[token] = weights.get(token, 0.0) + weight
            first_seen.setdefault(token, index)

    if not weights:
        return clean(" ".join(
            re.findall(r"\w+", clean(text))[-9:]
        ))[:80] or "हिंदी कहानी"

    ranked = sorted(
        weights,
        key=lambda token: (
            -weights[token],
            first_seen[token],
            token,
        ),
    )

    chosen = ranked[:4]

    # Include a distinctive concept from the ending.
    ending_units = units[max(0, int(total * 0.75)):]

    for sentence in ending_units:
        normalized = re.sub(
            r"[^\w\s]", " ", sentence.lower()
        )

        for token in normalized.split():
            if (
                len(token) > 2
                and token not in stop
                and token not in chosen
            ):
                chosen.append(token)
                break

        if len(chosen) >= 5:
            break

    title = " ".join(chosen[:5]).strip(
        " ।.!?！？—-:;,'\"“”‘’"
    )

    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0]

    return title or "हिंदी कहानी"


def split_balanced(text, count):
    """Split narration without losing, duplicating or reordering words."""
    units = (
        text if isinstance(text, list)
        else split_sentences(text)
    )
    units = [clean(item) for item in units if clean(item)]

    if not units:
        raise RuntimeError("Story has no usable sentences.")

    if count < 1:
        raise RuntimeError("Scene or part count must be positive.")

    if len(units) < count:
        tokens = re.findall(r"\S+", " ".join(units))

        if len(tokens) < count:
            raise RuntimeError(
                "Not enough story words for requested groups."
            )

        return [
            " ".join(tokens[
                round(i * len(tokens) / count):
                round((i + 1) * len(tokens) / count)
            ])
            for i in range(count)
        ]

    groups = []
    cursor = 0

    for index in range(count):
        remaining_groups = count - index

        if remaining_groups == 1:
            groups.append(" ".join(units[cursor:]))
            break

        remaining_words = sum(
            word_count(item) for item in units[cursor:]
        )
        target = remaining_words / remaining_groups
        max_take = len(units) - cursor - (remaining_groups - 1)

        group = []
        amount = 0

        while cursor < len(units) and len(group) < max_take:
            item = units[cursor]
            size = word_count(item)

            if (
                group
                and abs(amount - target)
                <= abs(amount + size - target)
            ):
                break

            group.append(item)
            amount += size
            cursor += 1

            if amount >= target:
                break

        if not group:
            group.append(units[cursor])
            cursor += 1

        groups.append(" ".join(group))

    if len(groups) != count or any(not clean(x) for x in groups):
        raise RuntimeError("Could not create requested story groups.")

    original_words = word_count(" ".join(units))
    grouped_words = sum(word_count(item) for item in groups)

    if original_words != grouped_words:
        raise RuntimeError("Balanced splitting changed the word count.")

    return groups


def _sentence_units_for_short(text):
    """Preserve sentence order; split only exceptionally long sentences."""
    units = split_sentences(text)
    result = []

    for sentence in units:
        tokens = re.findall(r"\S+", sentence)

        if len(tokens) <= SHORT_HARD_MAX_WORDS:
            result.append(sentence)
            continue

        for start in range(0, len(tokens), SHORT_TARGET_WORDS):
            chunk = tokens[start:start + SHORT_TARGET_WORDS]
            if chunk:
                result.append(" ".join(chunk))

    return result


def split_short_parts(text, count=None):
    """Create balanced Short parts from the complete narration."""
    units = _sentence_units_for_short(text)

    if not units:
        raise RuntimeError("Short story has no usable sentences.")

    prefix = [0]

    for item in units:
        prefix.append(prefix[-1] + word_count(item))

    total = prefix[-1]

    if count is None:
        count = calculate_dynamic_short_parts(total)

    if count < 1:
        raise RuntimeError("Dynamic SHORT part count must be positive.")

    if not (
        count * SHORT_MIN_WORDS
        <= total
        <= count * SHORT_HARD_MAX_WORDS
    ):
        raise RuntimeError(
            f"SHORT story has {total} words for {count} parts. "
            f"Valid range: {count * SHORT_MIN_WORDS}–"
            f"{count * SHORT_HARD_MAX_WORDS}."
        )

    @lru_cache(maxsize=None)
    def solve(start, remaining):
        if remaining == 1:
            amount = prefix[-1] - prefix[start]

            if SHORT_MIN_WORDS <= amount <= SHORT_HARD_MAX_WORDS:
                return abs(amount - SHORT_TARGET_WORDS), ()

            return None

        best = None
        last_end = len(units) - remaining + 1

        for end in range(start + 1, last_end + 1):
            amount = prefix[end] - prefix[start]

            if amount < SHORT_MIN_WORDS:
                continue

            if amount > SHORT_HARD_MAX_WORDS:
                break

            rest = solve(end, remaining - 1)

            if rest is None:
                continue

            candidate = (
                abs(amount - SHORT_TARGET_WORDS) + rest[0],
                (end,) + rest[1],
            )

            if best is None or candidate[0] < best[0]:
                best = candidate

        return best

    solution = solve(0, count)

    if solution is None:
        tokens = re.findall(r"\S+", " ".join(units))

        if len(tokens) != total:
            raise RuntimeError("SHORT word count became inconsistent.")

        boundaries = []
        remaining_words = total

        for part_index in range(count):
            remaining_parts = count - part_index

            if remaining_parts == 1:
                take = remaining_words
            else:
                target = round(remaining_words / remaining_parts)
                maximum = min(
                    SHORT_HARD_MAX_WORDS,
                    remaining_words
                    - SHORT_MIN_WORDS * (remaining_parts - 1),
                )
                take = max(
                    SHORT_MIN_WORDS,
                    min(target, maximum),
                )

            boundaries.append(take)
            remaining_words -= take

        result = []
        cursor = 0

        for take in boundaries:
            result.append(" ".join(tokens[cursor:cursor + take]))
            cursor += take

    else:
        boundaries = list(solution[1]) + [len(units)]
        result = []
        start = 0

        for end in boundaries:
            result.append(" ".join(units[start:end]).strip())
            start = end

    for number, part in enumerate(result, 1):
        amount = word_count(part)

        if not SHORT_MIN_WORDS <= amount <= SHORT_HARD_MAX_WORDS:
            raise RuntimeError(
                f"SHORT part {number} has {amount} words."
            )

    if sum(word_count(part) for part in result) != total:
        raise RuntimeError("SHORT rebalancing lost or duplicated words.")

    return result


def validate_plan(plan, mode, topic, parts_count, scenes_per_part):
    if not isinstance(plan, dict):
        raise RuntimeError("Story plan is missing or invalid.")

    if plan.get("status") != "planned":
        raise RuntimeError("Story plan status must be planned.")

    expected_total = parts_count * scenes_per_part

    expected_values = {
        "format": mode,
        "topic": topic,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_total,
        "scene_order": "part_then_scene",
        "mapping_version": 4,
    }

    for key, expected in expected_values.items():
        if plan.get(key) != expected:
            raise RuntimeError(
                f"Story plan mismatch: {key}; "
                f"expected {expected!r}, got {plan.get(key)!r}."
            )

    parts = plan.get("parts")

    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan has incorrect part count.")

    global_number = 0

    for part_number, part in enumerate(parts, 1):
        if (
            not isinstance(part, dict)
            or int(part.get("part", -1)) != part_number
        ):
            raise RuntimeError(f"Invalid story plan part {part_number}.")

        scenes = part.get("scenes")

        if (
            not isinstance(scenes, list)
            or len(scenes) != scenes_per_part
        ):
            raise RuntimeError(
                f"Story plan part {part_number} has incorrect scene count."
            )

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            scene_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid planned scene: {scene_id}.")

            if scene.get("scene_id") != scene_id:
                raise RuntimeError(f"Scene ID mismatch: {scene_id}.")

            if int(scene.get("scene", -1)) != scene_number:
                raise RuntimeError(f"Scene number mismatch: {scene_id}.")

            if int(scene.get("global_scene_number", -1)) != global_number:
                raise RuntimeError(
                    f"Global scene number mismatch: {scene_id}."
                )

            if not clean(scene.get("purpose")):
                raise RuntimeError(f"Scene purpose missing: {scene_id}.")

    return plan


def load_story_plan(mode, topic, parts_count, scenes_per_part):
    errors = []

    for path in PLAN_CANDIDATES:
        if not path.is_file():
            errors.append(f"{path.relative_to(ROOT)} missing")
            continue

        plan = read_json(path)

        if not isinstance(plan, dict):
            errors.append(f"{path.relative_to(ROOT)} invalid JSON")
            continue

        try:
            validate_plan(
                plan,
                mode,
                topic,
                parts_count,
                scenes_per_part,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")
            continue

        log("Using story plan:", path.relative_to(ROOT))
        return plan

    raise RuntimeError(
        "No valid story_plan.json found. " + "; ".join(errors)
    )


def selected_model():
    path = ROOT / "output" / "config" / "selected_model.json"
    data = read_json(path)
    model = clean(data.get("model")) if data else ""

    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(
            "No valid selected Gemini text model found. "
            "Check Select Gemini model workflow step."
        )

    return model


class GeminiRequestError(RuntimeError):
    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def gemini_api_key():
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )


def call_gemini_once(prompt, model, api_key):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model, safe="-._")
        + ":generateContent"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.35,
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
            result = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode(
                "utf-8",
                errors="replace",
            )[:1000]
        except Exception:
            detail = ""

        raise GeminiRequestError(
            f"HTTP {exc.code}: {detail}",
            status=exc.code,
            retryable=exc.code in RETRYABLE_HTTP,
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:
        raise GeminiRequestError(
            f"Network error: {type(exc).__name__}",
            retryable=True,
        ) from exc

    except json.JSONDecodeError as exc:
        raise GeminiRequestError(
            "Gemini returned invalid JSON.",
            retryable=True,
        ) from exc

    candidates = result.get("candidates", [])

    if not candidates:
        feedback = result.get("promptFeedback", {})
        raise GeminiRequestError(
            f"Gemini returned no candidate: {feedback}"
        )

    response_parts = (
        candidates[0].get("content", {}).get("parts", [])
    )

    response_text = "".join(
        item.get("text", "")
        for item in response_parts
        if isinstance(item, dict)
    ).strip()

    if not response_text:
        raise GeminiRequestError("Gemini returned an empty response.")

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise GeminiRequestError(
            "Gemini response was not valid JSON."
        ) from exc

    if not isinstance(parsed, dict):
        raise GeminiRequestError(
            "Gemini response must be a JSON object."
        )

    return parsed


def call_gemini(prompt, model, api_key):
    """Use only the configured model; retry transient failures."""
    errors = []

    for attempt in range(MAX_RETRIES_PER_MODEL):
        try:
            log(
                f"Gemini attempt: model={model}, "
                f"attempt={attempt + 1}/{MAX_RETRIES_PER_MODEL}"
            )

            result = call_gemini_once(prompt, model, api_key)
            log("Gemini story generation succeeded:", model)
            return result, model

        except GeminiRequestError as exc:
            errors.append(str(exc))
            log("Gemini request failed:", model, str(exc))

            if not exc.retryable:
                break

            if attempt + 1 < MAX_RETRIES_PER_MODEL:
                delay = RETRY_DELAYS[
                    min(attempt, len(RETRY_DELAYS) - 1)
                ]
                log("Retrying after", delay, "seconds.")
                time.sleep(delay)

    raise RuntimeError(
        f"Configured Gemini model {model} failed: "
        + " | ".join(errors)
    )


def _extract_narrative_text(row, number):
    if not isinstance(row, dict):
        raise RuntimeError(f"Invalid Gemini story part {number}.")

    try:
        returned_number = int(row.get("part", -1))
    except (TypeError, ValueError):
        returned_number = -1

    if returned_number != number:
        raise RuntimeError(
            f"Gemini part numbering mismatch at part {number}."
        )

    text = clean(
        row.get("text")
        or row.get("story")
        or row.get("narration")
    )

    if not text:
        raise RuntimeError(
            f"Gemini returned empty story part {number}."
        )

    return text


def build_generation_prompt(
    mode,
    topic,
    source_text,
    parts_count,
    config,
):
    if mode == "short":
        format_rules = """
SHORT REQUIREMENTS:
- Return the complete rewritten story in chronological order.
- The final number of Shorts parts is calculated by the program.
- Aim for approximately 135 words per final Short.
- Each final Short must be between 120 and 180 words.
- Do not remove important source events to meet a part limit.
- Give every part a story-specific opening hook.
- End non-final parts on a meaningful unresolved danger,
  discovery, decision or consequence from the source plot.
- Resolve the central conflict in the final part.
"""
    else:
        format_rules = f"""
FULL REQUIREMENTS:
- Return exactly {parts_count} numbered parts.
- Preserve the complete supplied story and its ending.
- Build a story-specific hook, escalating conflict and suspense.
- Resolve the central conflict in the final part.
"""

    return f"""
You are writing Hindi narration for a cinematic live-action story.

FORMAT: {mode}
REQUESTED PARTS: {parts_count}
AUDIENCE: {clean(str(config.get("AUDIENCE", "adult")))}
USER TITLE OR TOPIC: {topic or "(not supplied)"}

COMPLETE SOURCE STORY:
<source_story>
{source_text}
</source_story>

SOURCE-PRESERVATION RULES:
- Read the entire source, including its middle and ending.
- Preserve all important characters, relationships, locations,
  events, causes, consequences and the original ending.
- Do not replace the source plot with an unrelated story.
- Do not invent a new central conflict or unrelated characters.
- Do not change a character's identity, age, family relation,
  motivation or outcome.
- You may correct spelling, matras, punctuation and grammar.
- You may improve narration flow without changing meaning.
- If the source is already complete, do not unnecessarily expand it.
- If only a topic is supplied, create a complete original story.
- Do not include camera directions or image prompts in narration.
- Use natural Hindi Devanagari unless the source requires otherwise.
- Do not generate a second video format.

TITLE RULE:
- If a title/topic is supplied, use it as the story title.
- If no title/topic is supplied, derive the title from the complete
  story's central character, conflict, turning point and ending.
- Never select a title from only the first sentence.

HOOK AND SUSPENSE:
- Use a hook connected to the actual source plot.
- Build suspense through established clues and consequences.
- Avoid repetitive generic suspense lines.
- Do not fabricate a twist that is absent from the source.

FORMAT RULES:
{format_rules}

Return valid JSON only:
{{
  "title": "story title",
  "parts": [
    {{"part": 1, "text": "complete narration"}}
  ]
}}

No markdown fences and no extra keys.
"""


def generate_narrative(
    config,
    mode,
    topic,
    source_text,
    parts_count,
    model,
):
    api_key = gemini_api_key()

    # A supplied story must never be silently replaced by an
    # unrelated generated story when the API key is missing.
    if not api_key:
        if not source_text:
            raise RuntimeError(
                "A topic-only story requires GEMINI_API_KEY "
                "or GOOGLE_API_KEY."
            )

        if mode == "short":
            if word_count(source_text) < SHORT_MIN_WORDS:
                raise RuntimeError(
                    "Supplied SHORT story has fewer than 120 words. "
                    "Gemini is required to rewrite or expand it."
                )

            parts = split_short_parts(source_text, None)

        else:
            parts = split_balanced(source_text, parts_count)

        return {
            "title": topic or derive_title_from_story(source_text),
            "parts": parts,
            "generation_method": "offline_supplied_story_mapping",
            "model": "offline",
        }

    # For a supplied FULL story, preserve the user's text rather than
    # asking a model to rewrite its plot and potentially replace events.
    if source_text and mode == "full":
        parts = split_balanced(source_text, parts_count)

        return {
            "title": topic or derive_title_from_story(source_text),
            "parts": parts,
            "generation_method": "supplied_story_preserved",
            "model": "source",
        }

    source = source_text or (
        "(No supplied story. Create an original story from the topic.)"
    )

    prompt = build_generation_prompt(
        mode=mode,
        topic=topic,
        source_text=source,
        parts_count=parts_count,
        config=config,
    )

    data, used_model = call_gemini(prompt, model, api_key)

    returned_title = clean(data.get("title"))
    rows = data.get("parts")

    if (
        not isinstance(rows, list)
        or not rows
        or (mode == "full" and len(rows) != parts_count)
    ):
        raise RuntimeError(
            "Gemini returned an incorrect number of story parts."
        )

    raw_narratives = [
        _extract_narrative_text(row, number)
        for number, row in enumerate(rows, 1)
    ]

    if mode == "short":
        complete_story = "\n\n".join(raw_narratives)
        narratives = split_short_parts(complete_story, None)

        log("SHORT complete generated word count:", word_count(complete_story))
        log("SHORT final part count:", len(narratives))
        log(
            "SHORT final part word counts:",
            [word_count(item) for item in narratives],
        )
    else:
        narratives = raw_narratives

    title = (
        topic
        or returned_title
        or derive_title_from_story(source_text or "\n".join(narratives))
    )

    return {
        "title": title[:180],
        "parts": narratives,
        "generation_method": "gemini",
        "model": used_model,
    }

def make_story(
    config,
    plan,
    narrative,
    source_text,
    topic,
    model,
    mode,
    parts_count,
    scenes_per_part,
):
    title = clean(narrative.get("title") or topic)

    if not title:
        title = derive_title_from_story(
            source_text or "\n".join(
                str(item) for item in narrative.get("parts", [])
            )
        )

    actual_model = clean(narrative.get("model")) or model
    raw_parts = narrative.get("parts")

    if (
        not isinstance(raw_parts, list)
        or len(raw_parts) != parts_count
    ):
        raise RuntimeError(
            f"Story part mismatch: expected {parts_count}, "
            f"received {len(raw_parts) if isinstance(raw_parts, list) else 0}."
        )

    story_parts = []
    all_scenes = []
    global_scene = 0

    for part_index, raw_part in enumerate(raw_parts, 1):
        if isinstance(raw_part, dict):
            part_text = clean(
                raw_part.get("text")
                or raw_part.get("story")
                or raw_part.get("narration")
            )
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
                "expected 120–180."
            )

        scene_texts = split_balanced(
            part_text,
            scenes_per_part,
        )

        # Verify scene segmentation did not remove or duplicate words.
        if word_count(" ".join(scene_texts)) != amount:
            raise RuntimeError(
                f"Scene splitting changed narration in part {part_index}."
            )

        plan_scenes = plan["parts"][part_index - 1]["scenes"]
        part_scenes = []

        for scene_index, narration_text in enumerate(scene_texts, 1):
            global_scene += 1
            plan_scene = plan_scenes[scene_index - 1]

            scene_id = (
                f"part_{part_index:02d}_scene_{scene_index:02d}"
            )

            visual = clean(
                plan_scene.get("visual")
                or plan_scene.get("visual_prompt")
                or plan_scene.get("description")
            )

            anchor = clean(plan_scene.get("story_anchor"))

            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene. "
                    "Show the exact story moment described by this "
                    "narration: "
                    f"{narration_text}. "
                    f"Relevant source-story context: {anchor or topic}. "
                    "Maintain established character identity, clothing, "
                    "location, chronology and physical continuity. "
                    "Use natural human anatomy, realistic lighting, "
                    "credible objects and cinematic camera framing. "
                    "No cartoon, illustration or unrelated imagery."
                )

            purpose = clean(plan_scene.get("purpose"))

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
        "model": actual_model,
        "generation_method": narrative.get(
            "generation_method",
            "gemini_or_offline",
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


def validate_story(story, expected_parts, expected_scenes, mode):
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
        raise RuntimeError("Story part count validation failed.")

    if not isinstance(scenes, list):
        raise RuntimeError("Story scenes must be a list.")

    expected_total = expected_parts * expected_scenes

    if len(scenes) != expected_total:
        raise RuntimeError(
            f"Scene count mismatch: expected {expected_total}, "
            f"received {len(scenes)}."
        )

    global_index = 0
    flattened = []
    complete_parts_text = []

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

        if mode == "short" and not (
            SHORT_MIN_WORDS <= amount <= SHORT_HARD_MAX_WORDS
        ):
            raise RuntimeError(
                f"SHORT part {part_index} has {amount} words; "
                "expected 120–180."
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
                raise RuntimeError(
                    f"Scene ID mismatch: {expected_id}."
                )

            if int(scene.get("part", -1)) != part_index:
                raise RuntimeError(
                    f"Scene part mismatch: {expected_id}."
                )

            if int(scene.get("scene", -1)) != scene_index:
                raise RuntimeError(
                    f"Scene number mismatch: {expected_id}."
                )

            if int(scene.get("global_scene_number", -1)) != global_index:
                raise RuntimeError(
                    f"Global scene number mismatch: {expected_id}."
                )

            for field in ("narration", "visual", "visual_prompt"):
                if not clean(scene.get(field)):
                    raise RuntimeError(
                        f"Missing {field}: {expected_id}."
                    )

            if clean(scene.get("visual")) != clean(
                scene.get("visual_prompt")
            ):
                raise RuntimeError(
                    f"Visual prompt mismatch: {expected_id}."
                )

            scene_narrations.append(clean(scene["narration"]))
            flattened.append(scene)

        combined_scene_text = clean(" ".join(scene_narrations))

        if combined_scene_text != part_text:
            raise RuntimeError(
                f"Part {part_index} narration does not match "
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

    if int(story.get("total_scenes", -1)) != expected_total:
        raise RuntimeError("Story total_scenes metadata mismatch.")

    if int(story.get("total_parts", -1)) != expected_parts:
        raise RuntimeError("Story total_parts metadata mismatch.")

    if int(story.get("parts_count", -1)) != expected_parts:
        raise RuntimeError("Story parts_count metadata mismatch.")

    if int(story.get("total_story_words", -1)) != word_count(
        story.get("story_text", "")
    ):
        raise RuntimeError("Total story word-count metadata mismatch.")

    if int(story.get("source_word_count", -1)) != word_count(
        story.get("source_text", "")
    ):
        raise RuntimeError("Source story word-count metadata mismatch.")

    return True


def main():
    config_path = ROOT / "Input" / "topic.txt"

    if not config_path.is_file():
        raise RuntimeError(
            f"Missing configuration file: {config_path}"
        )

    config = load_and_validate(str(config_path))

    mode = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    source_text = get_complete_source(config, config_path)

    configured_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}.")

    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive.")

    if not topic and not source_text:
        raise RuntimeError(
            "Input/topic.txt must contain TOPIC or STORY_TEXT."
        )

    source_words = word_count(source_text)

    log("FORMAT:", mode)
    log("TOPIC:", topic or "(not supplied)")
    log("SUPPLIED STORY WORDS:", source_words)

    # A non-empty source that has unexpectedly become very short
    # must not silently continue as if it were the complete story.
    if source_text and source_words < 20:
        raise RuntimeError(
            "STORY_TEXT contains fewer than 20 words. "
            "Check the multiline STORY_TEXT block in Input/topic.txt."
        )

    if mode == "full":
        parts_count = configured_parts

        plan = load_story_plan(
            mode,
            topic,
            parts_count,
            scenes_per_part,
        )

    else:
        # The initial Short plan is provisional. The final plan is
        # rebuilt after the complete story has been rewritten and
        # its actual number of Shorts parts has been calculated.
        parts_count = configured_parts

        plan = build_story_plan(
            config=config,
            format_type=mode,
            topic=topic,
            story_text=source_text,
            parts_count=parts_count,
            scenes_per_part=scenes_per_part,
            dynamic_short=False,
        )

        validate_generated_plan(
            plan,
            mode,
            topic,
            parts_count,
            scenes_per_part,
        )

        write_json(STORY_DIR / "story_plan.json", plan)

    model = selected_model()
    STORY_DIR.mkdir(parents=True, exist_ok=True)

    fingerprint = digest({
        "version": VERSION,
        "format": mode,
        "configured_parts": configured_parts,
        "scenes": scenes_per_part,
        "topic": topic,
        "source_text": source_text,
        "model": model,
    })

    # Reuse only a complete output matching the exact current input.
    if OUTPUT_FILE.is_file():
        cached = read_json(OUTPUT_FILE)

        if (
            isinstance(cached, dict)
            and cached.get("source_fingerprint") == fingerprint
            and cached.get("status") == "completed"
        ):
            try:
                cached_parts = (
                    len(cached.get("parts", []))
                    if mode == "short"
                    else parts_count
                )

                validate_story(
                    cached,
                    cached_parts,
                    scenes_per_part,
                    mode,
                )

                log("Valid completed story found; reusing cached output.")
                log("Story API calls: 0")
                return

            except (RuntimeError, TypeError, ValueError) as exc:
                log("Existing story checkpoint invalid:", exc)

    narrative = generate_narrative(
        config=config,
        mode=mode,
        topic=topic,
        source_text=source_text,
        parts_count=parts_count,
        model=model,
    )

    if mode == "short":
        actual_parts = len(narrative.get("parts", []))

        if actual_parts < 1:
            raise RuntimeError(
                "Dynamic SHORT generation returned no parts."
            )

        for number, item in enumerate(narrative["parts"], 1):
            part_text = (
                clean(
                    item.get("text")
                    or item.get("story")
                    or item.get("narration")
                )
                if isinstance(item, dict)
                else clean(str(item))
            )

            amount = word_count(part_text)

            if not SHORT_MIN_WORDS <= amount <= SHORT_HARD_MAX_WORDS:
                raise RuntimeError(
                    f"SHORT part {number} has {amount} words; "
                    "expected 120–180."
                )

        story_source = "\n\n".join(
            clean(
                item.get("text")
                or item.get("story")
                or item.get("narration")
            )
            if isinstance(item, dict)
            else clean(str(item))
            for item in narrative["parts"]
        )

        parts_count = actual_parts

        plan = build_story_plan(
            config=config,
            format_type=mode,
            topic=topic,
            story_text=story_source,
            parts_count=parts_count,
            scenes_per_part=scenes_per_part,
            dynamic_short=True,
        )

        validate_generated_plan(
            plan,
            mode,
            topic,
            parts_count,
            scenes_per_part,
        )

        write_json(STORY_DIR / "story_plan.json", plan)

        log("Dynamic SHORT part count finalized:", parts_count)

    story = make_story(
        config=config,
        plan=plan,
        narrative=narrative,
        source_text=source_text,
        topic=topic,
        model=model,
        mode=mode,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
    )

    story["source_fingerprint"] = fingerprint

    validate_story(
        story,
        parts_count,
        scenes_per_part,
        mode,
    )

    write_json(OUTPUT_FILE, story)

    write_json(CACHE_FILE, {
        "version": VERSION,
        "source_fingerprint": fingerprint,
        "story": story,
    })

    atomic_write(
        TITLE_FILE,
        story["title"] + "\n",
    )

    log("STORY GENERATION: SUCCESS")
    log("Version:", VERSION)
    log("Format:", mode)
    log("Title:", story["title"])
    log("Source words:", story["source_word_count"])
    log("Generated words:", story["total_story_words"])
    log("Parts:", story["total_parts"])
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", story["total_scenes"])
    log("Generation method:", story["generation_method"])
    log("Actual model:", story["model"])
    log("Output:", OUTPUT_FILE)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("STORY GENERATION: FAILED")
        log(type(exc).__name__ + ":", str(exc))
        raise