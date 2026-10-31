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

VERSION = "story-builder-v19-short-part-auto-repair"
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

    response_parts = candidates[0].get("content", {}).get("parts", [])
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


def repair_short_parts(
    parts,
    invalid_numbers,
    source_text,
    title,
    model,
    api_key,
):
    invalid_numbers = sorted(set(int(x) for x in invalid_numbers))
    if not invalid_numbers:
        return parts, model

    existing = [
        {"part": index, "text": text}
        for index, text in enumerate(parts, 1)
    ]

    prompt = f"""
You are repairing a Hindi short-video story.