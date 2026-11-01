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
from pathlib import Path

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

STORY_DIR = ROOT / "output" / "story"
PLAN_FILE = STORY_DIR / "story_plan.json"
OUTPUT_FILE = STORY_DIR / "story.json"
CACHE_FILE = STORY_DIR / "story_cache.json"
TITLE_FILE = STORY_DIR / "final_title.txt"
INPUT_FILE = ROOT / "Input" / "topic.txt"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

VERSION = "story-builder-v24-source-preserving-fixed-parts"

SHORT_MIN_WORDS = 120
SHORT_MAX_WORDS = 150

MAX_RESPONSE_TOKENS = 16000
REQUEST_TIMEOUT = 180
MAX_RETRIES = 2
RETRY_DELAYS = (5, 12)
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


def log(*values):
    print(*values, flush=True)


def clean(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def word_count(value):
    return len(re.findall(r"\S+", clean(value)))


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


def read_story_text_direct(path):
    """Read the complete multiline STORY_TEXT block from Input/topic.txt."""
    file_path = Path(path)

    if not file_path.is_file():
        raise FileNotFoundError(
            f"Input configuration not found: {file_path}"
        )

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

            collected = []
            if remainder:
                collected.append(remainder)

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
            if value[0] in {"'", '"'}:
                return value[1:-1]

        return value

    return ""


def get_complete_source(config, input_path):
    """Never silently use a truncated STORY_TEXT parser result."""
    parsed = get_story_text(config)
    direct = read_story_text_direct(input_path)

    parsed_words = word_count(parsed)
    direct_words = word_count(direct)

    if direct_words > parsed_words:
        log(
            "STORY_TEXT parser mismatch:",
            f"parsed={parsed_words}",
            f"direct={direct_words}",
        )
        log("Using the complete STORY_TEXT block.")
        return direct

    if parsed_words and direct_words and parsed_words != direct_words:
        log(
            "STORY_TEXT word-count difference:",
            f"parsed={parsed_words}",
            f"direct={direct_words}",
        )

    return parsed or direct


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


def derive_title_from_story(text):
    """Create a deterministic fallback title using the complete story."""
    sentences = split_sentences(text)

    if not sentences:
        return "हिंदी कहानी"

    stop_words = {
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
        "कहानी", "कथा", "उस", "उन", "इन", "इसे", "उसे",
    }

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
                or token in stop_words
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

    selected = ranked[:4]

    # Add a distinctive word from the final quarter of the story.
    for sentence in sentences[max(0, int(total * 0.75)):]:
        tokens = re.sub(
            r"[^\w\s]", " ", sentence.lower()
        ).split()

        for token in tokens:
            if (
                len(token) > 2
                and token not in stop_words
                and token not in selected
            ):
                selected.append(token)
                break

        if len(selected) >= 5:
            break

    title = " ".join(selected[:5]).strip()

    return title[:80].rsplit(" ", 1)[0] if len(title) > 80 else title


def split_balanced(text, count):
    """Split text in order without dropping or duplicating words."""
    units = split_sentences(text)
    units = [clean(item) for item in units if clean(item)]

    if not units:
        raise RuntimeError("Story has no usable sentences.")

    if count < 1:
        raise RuntimeError("PARTS/SCENES must be greater than zero.")

    if len(units) < count:
        tokens = re.findall(r"\S+", " ".join(units))

        if len(tokens) < count:
            raise RuntimeError(
                "Not enough words for the requested number of groups."
            )

        groups = []
        for index in range(count):
            start = round(index * len(tokens) / count)
            end = round((index + 1) * len(tokens) / count)
            groups.append(" ".join(tokens[start:end]))

        return groups

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

            if group and abs(amount - target) <= abs(
                amount + size - target
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

    if len(groups) != count or any(not clean(item) for item in groups):
        raise RuntimeError("Could not create the requested story groups.")

    original_count = word_count(" ".join(units))
    grouped_count = sum(word_count(item) for item in groups)

    if original_count != grouped_count:
        raise RuntimeError(
            "Story splitting changed the original word count."
        )

    return groups


def validate_short_parts(parts):
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("SHORT story has no parts.")

    for number, item in enumerate(parts, 1):
        if isinstance(item, dict):
            text = clean(
                item.get("text")
                or item.get("story")
                or item.get("narration")
            )
        else:
            text = clean(str(item))

        count = word_count(text)

        if not SHORT_MIN_WORDS <= count <= SHORT_MAX_WORDS:
            raise RuntimeError(
                f"SHORT part {number} has {count} words. "
                f"Required range: {SHORT_MIN_WORDS}–{SHORT_MAX_WORDS}."
            )

    return True


def selected_model():
    data = read_json(MODEL_FILE)
    model = clean(data.get("model")) if data else ""

    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(
            "Selected Gemini model is missing or invalid. "
            "Run the Select Gemini model workflow step first."
        )

    return model


def gemini_api_key():
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )


class GeminiRequestError(RuntimeError):
    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def call_gemini_once(prompt, model, api_key):
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
            raw = response.read().decode("utf-8", errors="replace")

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        retryable = exc.code in RETRYABLE_HTTP
        raise GeminiRequestError(
            f"Gemini HTTP {exc.code}: {body[:1800]}",
            status=exc.code,
            retryable=retryable,
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:
        raise GeminiRequestError(
            f"Gemini network error: {exc}",
            retryable=True,
        ) from exc

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GeminiRequestError(
            "Gemini returned invalid response JSON."
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
        raise GeminiRequestError("Gemini returned an empty response.")

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise GeminiRequestError(
            "Gemini response body was not valid JSON."
        ) from exc

    if not isinstance(parsed, dict):
        raise GeminiRequestError(
            "Gemini response must be a JSON object."
        )

    return parsed


def call_gemini(prompt, model, api_key):
    """Retry only the selected model; do not silently change providers."""
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
    mode,
    topic,
    title,
    source_text,
    parts_count,
    config,
):
    if mode == "short":
        format_rules = f"""
SHORT RULES:
- Return exactly {parts_count} parts. Do not add or remove parts.
- Every part must contain 120–150 whitespace-separated words.
- Each part should be suitable for approximately one minute of narration.
- Preserve the complete source plot, characters, relationships,
  locations, events, causes, consequences and ending.
- Rewrite wording only to improve natural narration.
- Do not omit important events to fit the word limit.
- Each part needs a story-specific opening hook.
- Non-final parts should end on a source-grounded suspense point.
- The final part must resolve the central conflict if the source resolves it.
"""
    else:
        format_rules = f"""
FULL RULES:
- Return exactly {parts_count} numbered parts.
- Preserve the supplied story, its important events and original ending.
- Do not create a second SHORT version.
- Use a story-specific opening hook and a meaningful ending.
"""

    return f"""
You are writing natural Hindi narration for a realistic cinematic story.

FORMAT: {mode}
EXACT PART COUNT: {parts_count}
AUDIENCE: {clean(str(config.get("AUDIENCE", "adult")))}
EXPLICIT TITLE/TOPIC: {title or topic or "(not supplied)"}

COMPLETE SOURCE STORY:
<source_story>
{source_text}
</source_story>

MANDATORY SOURCE RULES:
- Read the entire source from beginning to ending before writing.
- Preserve the source's central meaning and complete sequence of events.
- Preserve every important character and their identity, relationships,
  motivations, decisions and final outcome.
- Preserve important locations, objects, clues and consequences.
- Do not replace the supplied story with an unrelated story.
- Do not invent a new central conflict, character or twist.
- Correct spelling, matras, punctuation and grammar where necessary.
- Improve sentence flow without changing the plot or meaning.
- Do not repeat the same information just to increase word count.
- Do not add camera directions, image prompts or production notes.
- Write natural Hindi Devanagari unless the source requires otherwise.

TITLE RULE:
- If an explicit title is supplied, use it.
- Otherwise understand the complete story and derive a concise title
  from its central conflict, important character and final revelation.
- Never use only the first sentence as the title.

HOOK AND SUSPENSE:
- Make the opening hook relevant to the actual story.
- Build suspense from real clues, danger and consequences in the source.
- Avoid generic repeated suspense phrases.
- Do not fabricate an ending or twist.

{format_rules}

Return valid JSON only:
{{
  "title": "story title",
  "parts": [
    {{"part": 1, "text": "complete narration"}}
  ]
}}

No markdown fences. No explanation outside JSON.
"""


def extract_part_text(row, number):
    if not isinstance(row, dict):
        raise RuntimeError(f"Invalid generated story part {number}.")

    try:
        returned_number = int(row.get("part", -1))
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
    )

    if not text:
        raise RuntimeError(
            f"Generated story part {number} is empty."
        )

    return text


def generate_narrative(
    config,
    mode,
    topic,
    title,
    source_text,
    parts_count,
    model,
):
    api_key = gemini_api_key()

    # Supplied FULL stories are preserved verbatim.
    if source_text and mode == "full":
        parts = split_balanced(source_text, parts_count)

        return {
            "title": title or derive_title_from_story(source_text),
            "parts": parts,
            "generation_method": "supplied_story_preserved",
            "model": "source",
        }

    # Without an API key, only use supplied text if it already fits
    # the exact requested SHORT part count and word limits.
    if not api_key:
        if not source_text:
            raise RuntimeError(
                "A topic-only story requires GEMINI_API_KEY "
                "or GOOGLE_API_KEY."
            )

        parts = split_balanced(source_text, parts_count)

        if mode == "short":
            validate_short_parts(parts)

        return {
            "title": title or derive_title_from_story(source_text),
            "parts": parts,
            "generation_method": "offline_supplied_story_mapping",
            "model": "offline",
        }

    prompt = build_generation_prompt(
        mode=mode,
        topic=topic,
        title=title,
        source_text=source_text or (
            "(No supplied story. Create a complete original story "
            f"from this topic: {topic})"
        ),
        parts_count=parts_count,
        config=config,
    )

    data = call_gemini(prompt, model, api_key)
    rows = data.get("parts")

    if not isinstance(rows, list) or len(rows) != parts_count:
        raise RuntimeError(
            f"Gemini must return exactly {parts_count} parts; "
            f"received {len(rows) if isinstance(rows, list) else 0}."
        )

    parts = [
        extract_part_text(row, number)
        for number, row in enumerate(rows, 1)
    ]

    if mode == "short":
        validate_short_parts(parts)

    returned_title = clean(data.get("title"))
    final_title = title or returned_title

    if not final_title:
        final_title = derive_title_from_story(
            source_text or "\n\n".join(parts)
        )

    return {
        "title": final_title[:180],
        "parts": parts,
        "generation_method": "gemini",
        "model": model,
    }

def make_story(
    config,
    plan,
    narrative,
    source_text,
    topic,
    mode,
    parts_count,
    scenes_per_part,
):
    title = clean(narrative.get("title"))

    if not title:
        title = derive_title_from_story(
            source_text
            or "\n\n".join(
                str(item) for item in narrative.get("parts", [])
            )
        )

    raw_parts = narrative.get("parts")

    if not isinstance(raw_parts, list) or len(raw_parts) != parts_count:
        raise RuntimeError(
            f"Story part mismatch: expected {parts_count}, "
            f"received {len(raw_parts) if isinstance(raw_parts, list) else 0}."
        )

    plan_parts = plan.get("parts")

    if not isinstance(plan_parts, list) or len(plan_parts) != parts_count:
        raise RuntimeError("Story plan part count does not match story.")

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
            SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS
        ):
            raise RuntimeError(
                f"SHORT part {part_index} has {amount} words; "
                f"expected {SHORT_MIN_WORDS}–{SHORT_MAX_WORDS}."
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

        plan_scenes = plan_parts[part_index - 1].get("scenes")

        if (
            not isinstance(plan_scenes, list)
            or len(plan_scenes) != scenes_per_part
        ):
            raise RuntimeError(
                f"Story plan scenes are invalid for part {part_index}."
            )

        part_scenes = []

        for scene_index, narration_text in enumerate(scene_texts, 1):
            global_scene += 1
            plan_scene = plan_scenes[scene_index - 1]

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
                    "Keep established character identity, age, face, "
                    "clothing, location, objects and chronology consistent. "
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
    story,
    expected_parts,
    expected_scenes,
    mode,
):
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

        if mode == "short" and not (
            SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS
        ):
            raise RuntimeError(
                f"SHORT part {part_index} has {amount} words; "
                f"required {SHORT_MIN_WORDS}–{SHORT_MAX_WORDS}."
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

            for field in (
                "narration",
                "visual",
                "visual_prompt",
            ):
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
    mode,
    topic,
    parts_count,
    scenes_per_part,
):
    if not PLAN_FILE.is_file():
        raise RuntimeError(
            "Required story_plan.json is missing. "
            "The Generate story plan workflow step must run first."
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
    config,
    topic,
    story_text,
    parts_count,
    scenes_per_part,
):
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
    cached,
    fingerprint,
    mode,
    parts_count,
    scenes_per_part,
):
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
    except (RuntimeError, TypeError, ValueError):
        return False


def main():
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

    configured_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT: {mode}."
        )

    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError(
            "PARTS and SCENES must be positive integers."
        )

    if not topic and not title_input and not source_text:
        raise RuntimeError(
            "Provide TOPIC, TITLE or STORY_TEXT in Input/topic.txt."
        )

    if source_text and word_count(source_text) < 20:
        raise RuntimeError(
            "STORY_TEXT contains fewer than 20 words. "
            "Check the complete multiline story in Input/topic.txt."
        )

    if mode == "short":
        parts_count = configured_parts
    else:
        parts_count = configured_parts

    api_key = gemini_api_key()

    if api_key and (mode == "short" or not source_text):
        model = selected_model()
    else:
        model = "source" if source_text else "offline"

    fingerprint = digest({
        "version": VERSION,
        "format": mode,
        "parts": parts_count,
        "scenes": scenes_per_part,
        "topic": topic,
        "title": title_input,
        "source_text": source_text,
        "model": model,
    })

    log("FORMAT:", mode)
    log("TITLE:", title_input or "(not supplied)")
    log("TOPIC:", topic or "(not supplied)")
    log("SUPPLIED STORY WORDS:", word_count(source_text))
    log("CONFIGURED PARTS:", parts_count)
    log("SCENES PER PART:", scenes_per_part)
    log("SELECTED MODEL:", model)

    # Reuse only a complete story matching the exact current input.
    cached = read_json(OUTPUT_FILE)

    if cache_is_valid(
        cached,
        fingerprint,
        mode,
        parts_count,
        scenes_per_part,
    ):
        if mode == "short":
            plan = build_short_story_plan(
                config,
                topic,
                cached["story_text"],
                parts_count,
                scenes_per_part,
            )
        else:
            plan = load_full_story_plan(
                mode,
                topic,
                parts_count,
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

    if mode == "full":
        plan = load_full_story_plan(
            mode,
            topic,
            parts_count,
            scenes_per_part,
        )
    else:
        # The final SHORT plan is built after narration is ready.
        plan = None

    narrative = generate_narrative(
        config=config,
        mode=mode,
        topic=topic,
        title=title_input,
        source_text=source_text,
        parts_count=parts_count,
        model=model,
    )

    actual_parts = narrative.get("parts")

    if not isinstance(actual_parts, list):
        raise RuntimeError("Narrative generator did not return a parts list.")

    if len(actual_parts) != parts_count:
        raise RuntimeError(
            f"PARTS mismatch: configured {parts_count}, "
            f"generated {len(actual_parts)}."
        )

    if mode == "short":
        validate_short_parts(actual_parts)

    story_text_for_plan = "\n\n".join(
        clean(
            item.get("text")
            or item.get("story")
            or item.get("narration")
        )
        if isinstance(item, dict)
        else clean(str(item))
        for item in actual_parts
    )

    if not story_text_for_plan:
        raise RuntimeError("Generated story text is empty.")

    if mode == "short":
        plan = build_short_story_plan(
            config,
            topic,
            story_text_for_plan,
            parts_count,
            scenes_per_part,
        )

    story = make_story(
        config=config,
        plan=plan,
        narrative=narrative,
        source_text=source_text,
        topic=topic,
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

    log("=" * 58)
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