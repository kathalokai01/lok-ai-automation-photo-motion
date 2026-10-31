#!/usr/bin/env python3
"""Katha Lok AI — resilient story generation and scene mapping."""

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
    ROOT / "output" / "config" / "story_plan.json",
    ROOT / "output" / "story" / "story_plan.json",
)
OUTPUT_FILE = STORY_DIR / "story.json"
CACHE_FILE = STORY_DIR / "story_cache.json"
TITLE_FILE = STORY_DIR / "final_title.txt"

VERSION = "story-builder-v18-gemini-retry-fallback"
SHORT_MIN_WORDS = 120
SHORT_MAX_WORDS = 150
SHORT_TARGET_WORDS = 135
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
        data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
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
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
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


def derive_title_from_story(text):
    """Offline title heuristic considers the complete story."""
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
    weights, first_seen = {}, {}
    total = len(units)

    for index, sentence in enumerate(units):
        normalized = re.sub(r"[^\w\s]", " ", sentence.lower())
        tokens = {
            token for token in normalized.split()
            if len(token) > 2 and token not in stop
            and not token.isdigit()
        }
        position = index / max(total - 1, 1)
        weight = 1.35 if position >= 0.80 else (
            1.25 if 0.20 <= position <= 0.80 else 1.0
        )
        for token in tokens:
            weights[token] = weights.get(token, 0.0) + weight
            first_seen.setdefault(token, index)

    if not weights:
        return clean(" ".join(re.findall(r"\w+", clean(text))[-9:]))[:80]

    ranked = sorted(
        weights,
        key=lambda token: (-weights[token], first_seen[token], token),
    )
    chosen = ranked[:4]
    ending_tokens = []

    for sentence in units[max(0, int(total * 0.75)):]:
        normalized = re.sub(r"[^\w\s]", " ", sentence.lower())
        for token in normalized.split():
            if (
                len(token) > 2 and token not in stop
                and token not in chosen and token not in ending_tokens
            ):
                ending_tokens.append(token)

    if ending_tokens and len(chosen) < 5:
        chosen.append(ending_tokens[0])

    title = " ".join(chosen[:5]).strip(" ।.!?！？—-:;,'\"“”‘’")
    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0]
    return title or "हिंदी कहानी"


def split_balanced(text, count):
    units = text if isinstance(text, list) else split_sentences(text)
    units = [clean(item) for item in units if clean(item)]

    if not units:
        raise RuntimeError("Story has no usable sentences.")
    if count < 1:
        raise RuntimeError("Scene or part count must be positive.")

    if len(units) < count:
        tokens = re.findall(r"\S+", " ".join(units))
        if len(tokens) < count:
            raise RuntimeError("Not enough story text for requested groups.")
        return [
            " ".join(tokens[
                round(i * len(tokens) / count):
                round((i + 1) * len(tokens) / count)
            ])
            for i in range(count)
        ]

    groups, cursor = [], 0
    for index in range(count):
        remaining_groups = count - index
        if remaining_groups == 1:
            groups.append(" ".join(units[cursor:]))
            break

        remaining_words = sum(word_count(x) for x in units[cursor:])
        target = remaining_words / remaining_groups
        max_take = len(units) - cursor - (remaining_groups - 1)
        group, amount = [], 0

        while cursor < len(units) and len(group) < max_take:
            item = units[cursor]
            size = word_count(item)
            if group and abs(amount - target) <= abs(amount + size - target):
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
    return groups


def split_short_parts(text, count):
    from functools import lru_cache

    units = split_sentences(text)
    if not units:
        raise RuntimeError("Short story has no usable sentences.")

    prefix = [0]
    for item in units:
        prefix.append(prefix[-1] + word_count(item))

    total = prefix[-1]
    if not count * SHORT_MIN_WORDS <= total <= count * SHORT_MAX_WORDS:
        raise RuntimeError(
            f"Short story has {total} words for {count} parts; "
            f"required total is {count * SHORT_MIN_WORDS}-"
            f"{count * SHORT_MAX_WORDS}. Rewrite required."
        )

    @lru_cache(maxsize=None)
    def solve(start, remaining):
        if remaining == 1:
            amount = prefix[-1] - prefix[start]
            if SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                return abs(amount - SHORT_TARGET_WORDS), ()
            return None

        best = None
        last_end = len(units) - remaining + 1
        for end in range(start + 1, last_end + 1):
            amount = prefix[end] - prefix[start]
            if amount < SHORT_MIN_WORDS:
                continue
            if amount > SHORT_MAX_WORDS:
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
        raise RuntimeError(
            "Cannot split into 120–150 word parts at sentence boundaries; "
            "Gemini rewrite is required."
        )

    boundaries = list(solution[1]) + [len(units)]
    result, start = [], 0
    for end in boundaries:
        result.append(" ".join(units[start:end]).strip())
        start = end

    for number, part in enumerate(result, 1):
        amount = word_count(part)
        if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
            raise RuntimeError(
                f"Short part {number} has {amount} words; expected 120–150."
            )
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
                f"Story plan mismatch: {key}; expected {expected!r}, "
                f"got {plan.get(key)!r}."
            )

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan has incorrect part count.")

    global_number = 0
    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != part_number:
            raise RuntimeError(f"Invalid story plan part {part_number}.")
        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Story plan part {part_number} has incorrect scene count."
            )
        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid planned scene: {scene_id}.")
            if scene.get("scene_id") != scene_id:
                raise RuntimeError(f"Scene ID mismatch: {scene_id}.")
            if int(scene.get("scene", -1)) != scene_number:
                raise RuntimeError(f"Scene number mismatch: {scene_id}.")
            if int(scene.get("global_scene_number", -1)) != global_number:
                raise RuntimeError(f"Global scene number mismatch: {scene_id}.")
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
        validate_plan(plan, mode, topic, parts_count, scenes_per_part)
        log("Using story plan:", path.relative_to(ROOT))
        return plan
    raise RuntimeError("No valid story_plan.json found. " + "; ".join(errors))


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


def list_fallback_models(api_key):
    """Fetch only models explicitly advertising generateContent support."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models?"
        + urllib.parse.urlencode({"pageSize": "100", "key": api_key})
    )
    found, seen = [], set()
    pages = 0

    while url and pages < 5:
        request = urllib.request.Request(
            url, headers={"Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            log("Fallback model discovery unavailable:", type(exc).__name__)
            break

        for item in data.get("models", []):
            if not isinstance(item, dict):
                continue
            resource = str(item.get("name", ""))
            methods = item.get("supportedGenerationMethods", [])
            if not resource.startswith("models/") or "generateContent" not in methods:
                continue
            name = resource.split("/", 1)[1]
            low = name.lower()
            if (
                not re.fullmatch(r"[A-Za-z0-9._-]+", name)
                or "gemini" not in low
                or any(x in low for x in (
                    "embedding", "image", "vision", "audio",
                    "tts", "robotics", "aqa",
                ))
                or name in seen
            ):
                continue
            seen.add(name)
            found.append(name)

        token = str(data.get("nextPageToken", "")).strip()
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models?"
            + urllib.parse.urlencode({
                "pageSize": "100", "pageToken": token, "key": api_key,
            })
        ) if token else ""
        pages += 1

    # Keep the selected model first; fallback order is stable and capability-based.
    def priority(name):
        low = name.lower()
        experimental = any(x in low for x in ("preview", "experimental", "exp-"))
        tier = (
            0 if "flash" in low and "lite" in low else
            1 if "flash" in low else
            2 if "pro" in low else 3
        )
        return (1 if experimental else 0, tier, low)

    return sorted(found, key=priority)


def call_gemini_once(prompt, model, api_key):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model, safe="-._")
        + ":generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.75,
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
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            detail = ""
        raise GeminiRequestError(
            f"HTTP {exc.code}: {detail}",
            status=exc.code,
            retryable=exc.code in RETRYABLE_HTTP,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
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
        raise GeminiRequestError("Gemini response must be a JSON object.")
    return parsed

def call_gemini(prompt, model, api_key):
    """Retry transient errors, then try API-listed compatible models."""
    candidates = [model]
    errors = []

    for candidate in candidates:
        for attempt in range(MAX_RETRIES_PER_MODEL):
            try:
                log(
                    f"Gemini attempt: model={candidate}, "
                    f"attempt={attempt + 1}/{MAX_RETRIES_PER_MODEL}"
                )
                result = call_gemini_once(prompt, candidate, api_key)
                log("Gemini story generation succeeded:", candidate)
                return result, candidate

            except GeminiRequestError as exc:
                errors.append(f"{candidate}: {exc}")
                log("Gemini request failed:", candidate, str(exc))

                if not exc.retryable:
                    break

                if attempt + 1 < MAX_RETRIES_PER_MODEL:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    log(f"Retrying after {delay} seconds.")
                    time.sleep(delay)

    # Discover fallback models only after the selected model fails.
    discovered = list_fallback_models(api_key)
    candidates.extend(x for x in discovered if x not in candidates)

    for candidate in candidates[1:]:
        try:
            log("Trying compatible Gemini fallback:", candidate)
            result = call_gemini_once(prompt, candidate, api_key)
            log("Gemini fallback succeeded:", candidate)
            return result, candidate
        except GeminiRequestError as exc:
            errors.append(f"{candidate}: {exc}")
            log("Gemini fallback failed:", candidate, str(exc))

    raise RuntimeError(
        "All available Gemini story-generation attempts failed. "
        + " | ".join(errors[-8:])
    )


def generate_narrative(
    config,
    mode,
    topic,
    source_text,
    parts_count,
    model,
):
    api_key = gemini_api_key()

    if not api_key:
        if not source_text:
            raise RuntimeError(
                "TOPIC-only story generation requires "
                "GEMINI_API_KEY or GOOGLE_API_KEY."
            )

        if mode == "short":
            parts = split_short_parts(source_text, parts_count)
        else:
            parts = split_balanced(source_text, parts_count)

        return {
            "title": topic or derive_title_from_story(source_text),
            "parts": parts,
            "generation_method": "offline_supplied_story_mapping",
            "model": "offline",
        }

    source = source_text or "(No supplied story. Create an original story.)"

    if mode == "short":
        mode_rules = (
            f"Return exactly {parts_count} parts. Every part must contain "
            "120–150 Hindi whitespace-separated words, targeting 135. "
            "Rewrite and condense supplied material as necessary. "
            "Do not merely split an overlong story. Do not pad with repetition. "
            "The opening needs an immediate story-specific hook. "
            "Each non-final part ends on a distinct unresolved danger, "
            "reveal or question. The final part resolves the central conflict."
        )
    else:
        mode_rules = (
            f"Return exactly {parts_count} parts. Create a complete story "
            "with a strong story-specific hook, consistent characters, "
            "credible cause-and-effect, escalating stakes and meaningful "
            "suspense. Resolve the central conflict in the final part."
        )

    prompt = f"""
You are the Hindi story writer for a cinematic live-action story video.

FORMAT: {mode}
REQUESTED PARTS: {parts_count}
USER TOPIC OR TITLE: {topic or "(no title supplied)"}
AUDIENCE: {clean(str(config.get("AUDIENCE", "adult")))}

COMPLETE SUPPLIED STORY:
<source_story>
{source}
</source_story>

TITLE RULE:
Read the complete supplied story, including its middle and ending.
Identify the central character, conflict, turning point, outcome and meaning.
Never choose a title using only the opening sentence.
If no topic/title is supplied, create a concise Hindi title based on
the complete story.

STORY RULES:
- Write natural, engaging Hindi narration.
- Preserve core events and relationships from supplied material.
- If only a topic is supplied, create a complete original story.
- Keep character identity, motivation, locations and chronology consistent.
- Start with a powerful, story-specific hook.
- Do not use generic openings such as "एक दिन की बात है".
- Build suspense using clues, risks and consequences.
- Avoid repetitive suspense phrases.
- Non-final parts should end at meaningful turning points.
- The final part must resolve the central conflict.
- Do not add scene directions or visual prompts to narration.
- Do not generate a second format.
- Use Hindi Devanagari unless the source requires otherwise.

FORMAT RULES:
{mode_rules}

Return valid JSON only:
{{
  "title": "complete-story title",
  "parts": [
    {{"part": 1, "text": "complete narration"}}
  ]
}}

No markdown fences and no extra keys.
"""

    data, used_model = call_gemini(prompt, model, api_key)
    title = clean(data.get("title"))
    rows = data.get("parts")

    if not title:
        if topic:
            title = topic
        elif source_text:
            title = derive_title_from_story(source_text)
        else:
            raise RuntimeError("Gemini did not return a story title.")

    if not isinstance(rows, list) or len(rows) != parts_count:
        raise RuntimeError(
            f"Gemini returned incorrect part count. Expected {parts_count}."
        )

    narratives = []
    for number, row in enumerate(rows, 1):
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

        text = clean(row.get("text"))
        if not text:
            raise RuntimeError(f"Gemini returned empty story part {number}.")

        if mode == "short":
            amount = word_count(text)
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"Short part {number} has {amount} words; "
                    "expected 120–150."
                )
        narratives.append(text)

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
            source_text or " ".join(narrative.get("parts", []))
        )

    actual_model = clean(narrative.get("model")) or model
    raw_parts = narrative.get("parts")

    if not isinstance(raw_parts, list) or len(raw_parts) != parts_count:
        raise RuntimeError(
            f"Story part mismatch: expected {parts_count}, "
            f"received {len(raw_parts) if isinstance(raw_parts, list) else 0}."
        )

    story_parts, all_scenes = [], []
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
            part_text = clean(raw_part)
            part_title = ""

        if not part_text:
            raise RuntimeError(f"Part {part_index} has empty narration.")

        if mode == "short":
            amount = word_count(part_text)
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"Short part {part_index} has {amount} words; "
                    "expected 120–150."
                )

        scene_texts = split_balanced(part_text, scenes_per_part)
        plan_scenes = plan["parts"][part_index - 1]["scenes"]
        part_scenes = []

        for scene_index, narration_text in enumerate(scene_texts, 1):
            global_scene += 1
            plan_scene = plan_scenes[scene_index - 1]
            scene_id = f"part_{part_index:02d}_scene_{scene_index:02d}"

            visual = clean(
                plan_scene.get("visual")
                or plan_scene.get("visual_prompt")
                or plan_scene.get("description")
            )
            anchor = clean(plan_scene.get("story_anchor"))

            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene. "
                    "Show the exact story moment described by this narration: "
                    f"{narration_text}. "
                    f"Relevant story context: {anchor or topic}. "
                    "Maintain established character identity, clothing, "
                    "location, chronology and physical continuity. "
                    "Natural human anatomy, realistic lighting and "
                    "cinematic camera framing. No cartoon or illustration."
                )

            scene_row = {
                "part": part_index,
                "scene": scene_index,
                "scene_id": scene_id,
                "global_scene_number": global_scene,
                "purpose": clean(plan_scene.get("purpose")),
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
                    or plan_scene.get("purpose")
                ),
            }
            part_scenes.append(scene_row)
            all_scenes.append(scene_row)

        story_parts.append({
            "part": part_index,
            "part_id": f"part_{part_index:02d}",
            "title": part_title or f"भाग {part_index}",
            "narration": part_text,
            "word_count": word_count(part_text),
            "scenes": part_scenes,
        })

    result = {
        "version": VERSION,
        "status": "completed",
        "title": title,
        "final_title": title,
        "topic": topic,
        "format": mode,
        "model": actual_model,
        "generation_method": narrative.get(
            "generation_method", "gemini_or_offline"
        ),
        "source_text": source_text,
        "story_text": "\n\n".join(
            part["narration"] for part in story_parts
        ),
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

    global_index, flattened = 0, []
    for part_index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part {part_index}.")
        if int(part.get("part", -1)) != part_index:
            raise RuntimeError(f"Story part numbering mismatch: {part_index}.")

        part_text = clean(part.get("narration"))
        if not part_text:
            raise RuntimeError(f"Part {part_index} narration is empty.")

        if mode == "short":
            amount = word_count(part_text)
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"Short part {part_index} has {amount} words."
                )

        part_scenes = part.get("scenes")
        if (
            not isinstance(part_scenes, list)
            or len(part_scenes) != expected_scenes
        ):
            raise RuntimeError(f"Part {part_index} scene count mismatch.")

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
                raise RuntimeError(
                    f"Global scene number mismatch: {expected_id}."
                )

            for field in ("narration", "visual", "visual_prompt"):
                if not clean(scene.get(field)):
                    raise RuntimeError(f"Missing {field}: {expected_id}.")
            flattened.append(scene)

    if flattened != scenes:
        raise RuntimeError(
            "Top-level scenes list differs from part scene records."
        )
    if int(story.get("total_scenes", -1)) != expected_total:
        raise RuntimeError("Story total_scenes metadata mismatch.")


def main():
    config_path = ROOT / "Input" / "topic.txt"
    if not config_path.is_file():
        raise RuntimeError(f"Missing configuration file: {config_path}")

    config = load_and_validate(str(config_path))
    mode = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    source_text = clean(get_story_text(config))
    parts_count = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {mode}")
    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive.")
    if not topic and not source_text:
        raise RuntimeError("Input/topic.txt must contain TOPIC or STORY_TEXT.")

    plan = load_story_plan(mode, topic, parts_count, scenes_per_part)
    model = selected_model()

    fingerprint = digest({
        "version": VERSION,
        "format": mode,
        "parts": parts_count,
        "scenes": scenes_per_part,
        "topic": topic,
        "source_text": source_text,
        "model": model,
        "plan": plan,
    })

    STORY_DIR.mkdir(parents=True, exist_ok=True)

    if OUTPUT_FILE.is_file():
        cached = read_json(OUTPUT_FILE)
        if (
            isinstance(cached, dict)
            and cached.get("source_fingerprint") == fingerprint
            and cached.get("status") == "completed"
        ):
            try:
                validate_story(cached, parts_count, scenes_per_part, mode)
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

    validate_story(story, parts_count, scenes_per_part, mode)
    write_json(OUTPUT_FILE, story)
    write_json(CACHE_FILE, {
        "version": VERSION,
        "source_fingerprint": fingerprint,
        "story": story,
    })
    atomic_write(TITLE_FILE, story["title"] + "\n")

    log("STORY GENERATION: SUCCESS")
    log("Version:", VERSION)
    log("Format:", mode)
    log("Title:", story["title"])
    log("Parts:", story["total_parts"])
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", story["total_scenes"])
    log("Generation method:", story["generation_method"])
    log("Actual model:", story["model"])
    log("Output:", OUTPUT_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log("ERROR:", exc, file=sys.stderr)
        sys.exit(1)