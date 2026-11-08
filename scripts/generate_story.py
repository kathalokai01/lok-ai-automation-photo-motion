#!/usr/bin/env python3
"""Correct a complete story, select SHORT Parts, and build a variable-Scene plan."""
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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    get_format, get_topic, get_story_text, get_parts_range,
    get_scenes_range, set_selected_parts, load_and_validate,
    get_title_source,
)
from select_gemini_model import configured_fallbacks
from story_length_policy import (
    parse_parts_range, build_length_policy, select_final_part_count,
    update_policy_with_corrected_story, validate_all_parts,
    verify_revision_evidence, SHORT_MIN_WORDS_PER_PART,
    SHORT_PREFERRED_MAX_WORDS_PER_PART, SHORT_HARD_MAX_WORDS_PER_PART,
    SHORT_FINAL_MERGED_MAX_WORDS,
)
from generate_story_plan import (
    build_plan as build_story_plan,
    validate_plan as validate_story_plan,
    split_sentences as plan_split_sentences,
    split_clauses as plan_split_clauses,
)

STORY_DIR = ROOT / "output" / "story"
OUTPUT_DIR = ROOT / "output"
INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT_FILE = STORY_DIR / "story.json"
CACHE_FILE = STORY_DIR / "story_cache.json"
CHECKPOINT_FILE = STORY_DIR / "story_checkpoint.json"
CORRECTED_STORY_FILE = STORY_DIR / "corrected_complete_story.txt"
PLAN_FILE = STORY_DIR / "story_plan.json"
TITLE_FILE = STORY_DIR / "final_title.txt"
TITLE_CACHE_FILE = STORY_DIR / "title_cache.json"
POLICY_FILE = STORY_DIR / "story_length_policy.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

VERSION = "story-builder-v35-pasted-story-authority-and-title-topic-metadata"
SHORT_MIN = 120
SHORT_TARGET = 135
SHORT_MAX = 150
SHORT_HARD_MAX = 180
SHORT_MERGED_MAX = 239
REVISION_ATTEMPTS = 3
MAX_TOKENS = 16000
TIMEOUT = 180
RETRY_DELAYS = (5, 12)
MAX_PARTS = 1000
MAX_TOTAL_SCENES = 1000
MAX_DP_OPERATIONS = 15000000


def log(*values: Any) -> None:
    print(*values, flush=True)


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_text(value: Any) -> str:
    value = str(value or "").replace("\ufeff", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def word_count(value: Any) -> int:
    return len(re.findall(r"\S+", clean(value)))


def sha256(value: Any) -> str:
    return hashlib.sha256(clean(value).encode("utf-8")).hexdigest()


def fingerprint(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def read_complete_story_assignment(path: Path) -> str:
    """Read a complete multiline STORY_TEXT assignment from Input/topic.txt."""
    if not path.is_file():
        return ""

    lines = path.read_text(encoding="utf-8-sig").splitlines()

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
            closing = remainder.find(delimiter)

            if closing >= 0:
                trailing = remainder[closing + len(delimiter):].strip()
                if trailing and not trailing.startswith("#"):
                    raise RuntimeError(
                        "Unexpected content after STORY_TEXT closing delimiter."
                    )
                return remainder[:closing].strip()

            collected = [remainder] if remainder else []

            for current in lines[index + 1:]:
                closing = current.find(delimiter)
                if closing >= 0:
                    if current[:closing]:
                        collected.append(current[:closing])

                    trailing = current[closing + len(delimiter):].strip()
                    if trailing and not trailing.startswith("#"):
                        raise RuntimeError(
                            "Unexpected content after STORY_TEXT closing delimiter."
                        )

                    return "\n".join(collected).strip()

                collected.append(current)

            raise RuntimeError(
                "STORY_TEXT multiline block has no closing delimiter."
            )

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            return value[1:-1]

        return value

    return ""


def get_complete_source(config: dict) -> str:
    """Public API used by verify_story.py."""
    parsed = normalize_text(get_story_text(config))
    direct = normalize_text(read_complete_story_assignment(INPUT_FILE))

    return (
        direct
        if word_count(direct) > word_count(parsed)
        else (parsed or direct)
    )


def split_sentences(text: str) -> list[str]:
    return [
        clean(item)
        for item in plan_split_sentences(text)
        if clean(item)
    ]


def split_clauses(sentence: str) -> list[str]:
    return [
        clean(item)
        for item in plan_split_clauses(sentence)
        if clean(item)
    ]


def _balanced_unit_partition(
    units: list[str],
    part_count: int,
    scenes_min: int,
    scenes_max: int,
    *,
    allow_final_short: bool = True,
    final_max: int = SHORT_HARD_MAX,
) -> list[str] | None:
    """Contiguous DP partition; sentence/clause order is never changed."""
    total_units = len(units)

    if (
        part_count < 1
        or total_units < part_count * scenes_min
        or total_units > part_count * scenes_max
    ):
        return None

    prefix = [0]
    for unit in units:
        prefix.append(prefix[-1] + word_count(unit))

    states: dict[int, tuple[float, list[int]]] = {
        0: (0.0, [0])
    }
    operations = 0

    for part_index in range(part_count):
        next_states: dict[int, tuple[float, list[int]]] = {}
        remaining_parts = part_count - part_index - 1
        is_final = remaining_parts == 0

        for start, (base_cost, boundaries) in states.items():
            min_take = max(
                scenes_min,
                total_units - start - remaining_parts * scenes_max,
            )
            max_take = min(
                scenes_max,
                total_units - start - remaining_parts * scenes_min,
            )

            for take in range(min_take, max_take + 1):
                operations += 1

                if operations > MAX_DP_OPERATIONS:
                    raise RuntimeError(
                        "Story segmentation exceeded the safe planning limit; "
                        "reduce PARTS/SCENES range or shorten the story."
                    )

                end = start + take
                amount = prefix[end] - prefix[start]

                if is_final:
                    if amount < 1 or amount > final_max:
                        continue
                    if amount < SHORT_MIN and not allow_final_short:
                        continue
                elif not SHORT_MIN <= amount <= SHORT_HARD_MAX:
                    continue

                segment_cost = abs(amount - SHORT_TARGET)

                if amount > SHORT_MAX:
                    segment_cost += (amount - SHORT_MAX) * 0.45

                if is_final and amount < SHORT_MIN:
                    segment_cost += 180 + (SHORT_MIN - amount) * 2

                candidate = base_cost + segment_cost
                current = next_states.get(end)

                if current is None or candidate < current[0]:
                    next_states[end] = (
                        candidate,
                        boundaries + [end],
                    )

        states = next_states

        if not states:
            return None

    final = states.get(total_units)
    if final is None:
        return None

    boundaries = final[1]
    groups = [
        "\n".join(units[boundaries[i]:boundaries[i + 1]])
        for i in range(part_count)
    ]

    if any(not clean(group) for group in groups):
        return None

    if clean(" ".join(groups)) != clean(" ".join(units)):
        raise RuntimeError(
            "Natural Part segmentation lost or reordered story material."
        )

    return groups


def _expand_to_scene_minimum(
    sentences: list[str],
    required_units: int,
) -> list[str]:
    """Split only meaningful complete clauses when sentence count is too small."""
    units = list(sentences)

    if len(units) >= required_units:
        return units

    expanded: list[str] = []

    for sentence in sentences:
        pieces = split_clauses(sentence)
        expanded.extend(
            pieces if len(pieces) > 1 else [sentence]
        )

    if len(expanded) < required_units:
        raise RuntimeError(
            f"Only {len(expanded)} complete sentence/meaningful-clause units "
            f"exist; selected PARTS × SCENES minimum requires {required_units}. "
            "Mid-sentence word splitting is forbidden."
        )

    if clean(" ".join(expanded)) != clean(" ".join(sentences)):
        raise RuntimeError(
            "Clause expansion changed the complete story text."
        )

    return expanded


def split_balanced(
    text: str,
    count: int,
    scenes_range: tuple[int, int] | None = None,
) -> list[str]:
    """Compatibility splitter using complete natural units and variable Scenes."""
    if isinstance(count, bool) or int(count) < 1:
        raise RuntimeError("Part/Scene count must be positive.")

    count = int(count)
    sentences = split_sentences(text)

    if not sentences:
        raise RuntimeError("Story contains no complete sentence.")

    if scenes_range is None:
        units = sentences

        if len(units) < count:
            units = _expand_to_scene_minimum(units, count)

        if len(units) < count:
            raise RuntimeError(
                "Insufficient natural units for requested group count."
            )

        groups = []
        cursor = 0
        remaining_words = sum(word_count(item) for item in units)

        for index in range(count):
            remaining_groups = count - index
            remaining_units = len(units) - cursor

            if remaining_groups == 1:
                groups.append("\n".join(units[cursor:]))
                break

            target = remaining_words / remaining_groups
            max_take = remaining_units - remaining_groups + 1
            selected = []
            selected_words = 0

            while cursor < len(units) and len(selected) < max_take:
                candidate = units[cursor]
                size = word_count(candidate)

                if (
                    selected
                    and abs(selected_words - target)
                    <= abs(selected_words + size - target)
                ):
                    break

                selected.append(candidate)
                selected_words += size
                cursor += 1

                if selected_words >= target:
                    break

            groups.append("\n".join(selected))
            remaining_words -= selected_words

        if len(groups) != count:
            raise RuntimeError(
                "Natural segmentation did not produce the requested groups."
            )

    else:
        scenes_min, scenes_max = scenes_range
        units = _expand_to_scene_minimum(
            sentences,
            count * scenes_min,
        )
        groups = _balanced_unit_partition(
            units,
            count,
            scenes_min,
            scenes_max,
        )

    if groups is None:
        raise RuntimeError(
            "No natural sentence/clause partition satisfies the selected "
            "PARTS, SCENES range and SHORT word limits. Adjust the configured ranges."
        )

    return groups


def _partition_short_story(
    story_text: str,
    preferred_count: int,
    minimum_parts: int,
    maximum_parts: int,
    scenes_range: tuple[int, int],
) -> tuple[list[str], int, bool]:
    """Find a valid contiguous partition; final merged Part may use 239 words."""
    scenes_min, scenes_max = scenes_range
    sentences = split_sentences(story_text)

    if not sentences:
        raise RuntimeError(
            "Complete corrected story contains no complete sentences."
        )

    candidates: list[int] = []

    for number in range(minimum_parts, maximum_parts + 1):
        if number * scenes_max <= MAX_TOTAL_SCENES:
            candidates.append(number)

    candidates.sort(
        key=lambda value: (abs(value - preferred_count), value)
    )

    last_errors = []
    expanded_units = []

    for sentence in sentences:
        pieces = split_clauses(sentence)
        expanded_units.extend(
            pieces if len(pieces) > 1 else [sentence]
        )

    variants = [sentences]

    if (
        expanded_units != sentences
        and clean(" ".join(expanded_units)) == clean(" ".join(sentences))
    ):
        variants.append(expanded_units)

    for units_source in variants:
        for part_count in candidates:
            units = list(units_source)
            required = part_count * scenes_min

            if len(units) < required:
                try:
                    units = _expand_to_scene_minimum(
                        units,
                        required,
                    )
                except RuntimeError as exc:
                    last_errors.append(str(exc))
                    continue

            if len(units) > part_count * scenes_max:
                continue

            groups = _balanced_unit_partition(
                units,
                part_count,
                scenes_min,
                scenes_max,
                allow_final_short=False,
                final_max=SHORT_HARD_MAX,
            )

            if groups is not None:
                return groups, part_count, False

    merged_candidates = [
        number for number in range(minimum_parts, maximum_parts + 1)
        if number >= 1
        and number < preferred_count
        and number * scenes_max <= MAX_TOTAL_SCENES
    ]
    merged_candidates.sort(
        key=lambda value: (
            abs(value - max(minimum_parts, preferred_count - 1)),
            value,
        )
    )

    for units_source in variants:
        for part_count in merged_candidates:
            units = list(units_source)
            required = part_count * scenes_min

            if len(units) < required:
                try:
                    units = _expand_to_scene_minimum(units, required)
                except RuntimeError as exc:
                    last_errors.append(str(exc))
                    continue

            if len(units) > part_count * scenes_max:
                continue

            groups = _balanced_unit_partition(
                units,
                part_count,
                scenes_min,
                scenes_max,
                allow_final_short=False,
                final_max=SHORT_MERGED_MAX,
            )

            if groups is not None:
                return groups, part_count, True

    detail = " | ".join(last_errors[-3:])
    raise RuntimeError(
        "Could not divide the corrected story into the configured PARTS and "
        f"SCENES ranges without cutting a sentence or violating SHORT limits. {detail}"
    )


def get_selected_model() -> str:
    data = read_json(MODEL_FILE)
    model = clean(data.get("model")) if data else ""

    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(
            "Configured selected Gemini model is missing or invalid."
        )

    return model


def get_configured_model_order() -> list[str]:
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError("Configured Gemini selector output is missing or invalid.")

    ordered = configured_fallbacks(data)
    if not ordered:
        raise RuntimeError("No configured Gemini model or fallback is available.")

    normalized = []
    for value in ordered:
        candidate = clean(value)
        if not re.fullmatch(r"gemini-[A-Za-z0-9._-]+", candidate, re.IGNORECASE):
            raise RuntimeError(f"Invalid configured Gemini model: {candidate!r}.")
        if candidate in normalized:
            raise RuntimeError(f"Duplicate Gemini model in fallback order: {candidate}.")
        normalized.append(candidate)

    saved_order = data.get("fallback_order")
    if not isinstance(saved_order, list) or [
        clean(item) for item in saved_order
    ] != normalized:
        raise RuntimeError("Saved Gemini fallback order does not match selector priority.")

    if clean(data.get("model")) != normalized[0]:
        raise RuntimeError("Saved Gemini primary model is not first in configured order.")

    return normalized


def save_selected_model(model: str, reason: str = "") -> None:
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError("Cannot save runtime Gemini result without selector state.")

    configured_order = data.get("fallback_order")
    if not isinstance(configured_order, list) or model not in configured_order:
        raise RuntimeError(
            "Runtime Gemini model is not present in the configured fallback order."
        )

    # Preserve the selector's configured primary and fallback sequence.
    # Runtime success is recorded separately and never changes model priority.
    data["runtime_fallback"] = {
        "status": "succeeded",
        "model": model,
        "reason": reason,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_json(MODEL_FILE, data)


def get_api_key() -> str:
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )


class ModelError(RuntimeError):
    pass


def _load_configured_provider_order() -> list[str]:
    """Load every locally validated configured non-Gemini provider."""
    selection_path = ROOT / "output" / "config" / "selected_llm_provider.json"
    selection = read_json(selection_path)

    if not isinstance(selection, dict):
        log("External LLM fallback unavailable: selector output is missing.")
        return []

    if selection.get("enabled") is not True or selection.get("status") != "ready":
        log("External LLM fallback unavailable: no configured provider is ready.")
        return []

    allowed = {"groq", "openrouter", "cloudflare", "mistral"}
    order = selection.get("provider_order")
    if not isinstance(order, list):
        return []

    result = []
    for value in order:
        provider = clean(value).lower()
        if provider in allowed and provider not in result:
            result.append(provider)
    return result



def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ModelError(
                f"Model JSON contains duplicate key: {key!r}."
            )
        result[key] = value
    return result


def _reject_nonfinite_json(value: str):
    raise ModelError(
        f"Model JSON contains unsupported non-finite value: {value}."
    )


def parse_model_response(raw: str) -> dict:
    value = str(raw or "").strip()
    value = re.sub(
        r"^\s*```(?:json)?\s*|\s*```$",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()

    if not value:
        raise ModelError("Model returned an empty response.")

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
        if isinstance(parsed, dict):
            return parsed
    except ModelError:
        raise
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_nonfinite_json,
    )

    for index, char in enumerate(value):
        if char != "{":
            continue

        try:
            parsed, _ = decoder.raw_decode(value[index:])
            if isinstance(parsed, dict):
                return parsed
        except ModelError:
            raise
        except json.JSONDecodeError:
            continue

    # A response that starts like JSON but is malformed must not be
    # mistaken for plain-text narration and saved as the complete story.
    if value.startswith(("{", "[")):
        raise ModelError("Model returned malformed or unsupported JSON.")

    plain = normalize_text(value)
    if not plain:
        raise ModelError(
            "Model returned neither usable JSON nor plain text."
        )

    return {
        "title": "",
        "story_text": plain,
        "parts": [],
        "response_format": "plain_text",
    }

def call_model(
    prompt: str,
    model: str,
    api_key: str,
    *,
    max_tokens: int = MAX_TOKENS,
) -> dict:
    """Try configured Gemini models first, then every ready external LLM provider."""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.25,
            "maxOutputTokens": max_tokens,
        },
    }
    failures = []
    configured_models = get_configured_model_order()
    retryable_http = {408, 429, 500, 502, 503, 504}

    gemini_authorized = (
        os.environ.get("ALLOW_GEMINI_GENERATION", "").strip()
        == "I_ACCEPT_POTENTIAL_GEMINI_CHARGES"
    )

    if api_key and configured_models and gemini_authorized:
        from gemini_api_guard import require_gemini_generation
        api_key = require_gemini_generation("story generation", api_key=api_key)
        for model_index, candidate_model in enumerate(configured_models):
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + urllib.parse.quote(candidate_model, safe="-._")
                + ":generateContent"
            )
            model_errors = []

            for attempt in range(len(RETRY_DELAYS) + 1):
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
                    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                        response_data = json.loads(
                            response.read().decode("utf-8", errors="replace")
                        )

                    candidates = response_data.get("candidates") or []
                    if not candidates:
                        raise ModelError(f"{candidate_model}: no response candidate.")

                    content_parts = candidates[0].get("content", {}).get("parts", [])
                    response_text = "".join(
                        item.get("text", "")
                        for item in content_parts
                        if isinstance(item, dict)
                    ).strip()

                    if not response_text:
                        raise ModelError(f"{candidate_model}: empty response.")

                    parsed = parse_model_response(response_text)
                    if not isinstance(parsed, dict):
                        raise ModelError(f"{candidate_model}: invalid parsed response.")

                    if candidate_model != clean(model):
                        log(f"Runtime Gemini fallback succeeded: {candidate_model}")

                    save_selected_model(
                        candidate_model,
                        reason=(
                            "Primary Gemini model succeeded."
                            if model_index == 0
                            else "Earlier configured Gemini model failed; "
                                 "this configured fallback succeeded."
                        ),
                    )
                    parsed["_katha_model_used"] = candidate_model
                    parsed["_katha_provider_used"] = "gemini"
                    parsed["_katha_generation_method"] = "configured_gemini"
                    return parsed

                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", errors="replace")
                    model_errors.append(f"HTTP {exc.code}: {body[:1000]}")
                    if exc.code not in retryable_http:
                        break

                except (
                    urllib.error.URLError,
                    TimeoutError,
                    OSError,
                    ModelError,
                    json.JSONDecodeError,
                ) as exc:
                    model_errors.append(f"{type(exc).__name__}: {exc}")
                    if isinstance(exc, (ModelError, json.JSONDecodeError)):
                        break

                if attempt < len(RETRY_DELAYS):
                    delay = RETRY_DELAYS[attempt]
                    log(
                        f"Transient Gemini failure on {candidate_model}; "
                        f"retrying in {delay}s ({attempt + 1}/{len(RETRY_DELAYS)})."
                    )
                    time.sleep(delay)

            failures.append(
                f"Gemini {candidate_model}: " + " | ".join(model_errors)
            )
            log(
                f"Gemini model failed: {candidate_model}; "
                "trying next configured model."
            )
    elif api_key and configured_models:
        log("Gemini skipped: explicit billable-generation opt-in is absent.")
        failures.append("Gemini billing opt-in is absent; external providers will be tried.")
    else:
        failures.append("Gemini API key or configured Gemini models are absent.")

    provider_order = _load_configured_provider_order()
    if not provider_order:
        raise RuntimeError(
            "All configured Gemini models failed and no configured external LLM provider is ready. "
            + " || ".join(failures)
        )

    try:
        from llm_provider_client import ProviderError, generate_text
    except Exception as exc:
        raise RuntimeError(
            "External LLM provider client import failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    for provider in provider_order:
        try:
            generated = generate_text(
                provider,
                prompt,
                max_tokens=max(1, min(int(max_tokens), 32768)),
                temperature=0.25,
                timeout=TIMEOUT,
            )
            response_text = clean(generated.get("text"))
            if not response_text:
                raise ModelError(f"{provider}: provider returned empty text.")

            parsed = parse_model_response(response_text)
            parsed["_katha_model_used"] = (
                f"{provider}:{clean(generated.get('model'))}"
            )
            parsed["_katha_provider_used"] = provider
            parsed["_katha_generation_method"] = "external_provider_fallback"

            log(
                f"External LLM fallback succeeded: {provider}/"
                f"{clean(generated.get('model'))}"
            )
            return parsed

        except Exception as exc:
            failures.append(f"{provider}: {type(exc).__name__}: {exc}")
            log(
                f"LLM provider failed: {provider}; "
                "trying next validated provider."
            )

    raise RuntimeError(
         "All configured Gemini models and external LLM providers failed. " + " || ".join(failures)
    )


def build_story_prompt(
    mode: str,
    topic: str,
    title: str,
    source_text: str,
    config: dict,
) -> str:
    has_pasted_story = bool(normalize_text(source_text))
    task = (
        "Proofread and correct ONLY the supplied complete story; do not write a new story."
        if has_pasted_story
        else "Write a complete original Hindi story from the topic/title because no STORY_TEXT was supplied."
    )

    return f"""
You are a professional Hindi story proofreader and editor.

TASK: {task}
FORMAT: {mode}
TOPIC (metadata/subject label only): {topic or '(not supplied)'}
EXPLICIT TITLE (story name only): {title or '(not supplied)'}
AUDIENCE: {config.get('AUDIENCE', 'adult')}

SOURCE PRIORITY — ABSOLUTE:
- If SOURCE STORY below contains text, it is the authoritative story.
- TITLE and TOPIC are labels/metadata only. They are NOT permission or instructions
  to invent, replace, restart, expand, or write a different story.
- When SOURCE STORY is present, edit that exact story only. Never create a new
  story from TITLE or TOPIC, even if either field is filled.
- Generate an original story from TITLE/TOPIC only when SOURCE STORY is empty.

Read the COMPLETE source from beginning to ending before editing.

SOURCE STORY:
<source_story>
{source_text}
</source_story>

MANDATORY RULES:
1. When a source story is present, retain its complete storyline and edit only
   spelling, Hindi matras, grammar, punctuation, and clarity.
2. Preserve every character, relationship, location, object, clue, event,
   motivation, chronology, dialogue meaning, consequence, and ending.
3. Do not invent, remove, replace, reorder, or add events, characters, dialogue,
   clues, locations, or outcomes.
4. Do not turn the source into a different story, summary, outline, or adaptation.
5. Do not let TITLE or TOPIC override, reinterpret, or replace SOURCE STORY.
6. If a correction could change meaning, retain the original wording instead.
7. Use natural Hindi Devanagari unless the source requires otherwise.
8. Return the entire corrected source story, not a summary.
9. If TITLE is blank and a source story exists, derive a title only after reading
   the entire story, including its middle and ending.
10. Return JSON with title and story_text. Plain text is accepted.

Return:
{{"title":"story title","story_text":"complete corrected source story"}}
"""


def generate_complete_story(
    mode: str,
    topic: str,
    title: str,
    source_text: str,
    config: dict,
    model: str,
    api_key: str,
) -> dict:
    result = call_model(
        build_story_prompt(
            mode,
            topic,
            title,
            source_text,
            config,
        ),
        model,
        api_key,
    )

    complete = normalize_text(
        result.get("story_text")
        or result.get("story")
        or result.get("narration")
        or result.get("text")
    )

    if not complete and isinstance(result.get("parts"), list):
        complete = normalize_text(
            "\n\n".join(
                str(
                    row.get("text", "")
                    if isinstance(row, dict)
                    else row
                )
                for row in result["parts"]
            )
        )

    if word_count(complete) < 20:
        raise RuntimeError(
            "Model did not return a complete usable story."
        )
    if source_text and clean(complete) == clean(source_text):
        log("Full story proofreading returned unchanged text; source is retained.")
    if not isinstance(result.get("title", ""), str):
        raise RuntimeError("Generated story title must be text.")

    source_verification = None

    if source_text:
        source_verification = verify_revision_semantics(
            source_text,
            complete,
            source_text,
            model,
            api_key,
        )

        if not source_verification.get("passed"):
            raise RuntimeError(
                "Corrected full story failed event/chronology "
                "preservation verification: "
                + clean(
                    source_verification.get("explanation")
                )
            )

    return {
        "story_text": complete,
        "title": clean(result.get("title")),
        "generation_method": (
            clean(result.get("_katha_generation_method"))
            or "configured_gemini"
        ),
        "model": (
            clean(source_verification.get("model"))
            if source_verification
            else clean(result.get("_katha_model_used")) or get_selected_model()
        ),
        "proofreading": {
            "performed": True,
            "method": (
                clean(result.get("_katha_generation_method"))
                or "configured_gemini_model"
            ),
            "generation_method": (
                clean(result.get("_katha_generation_method"))
                or "configured_gemini"
            ),
            "complete_story_reviewed": True,
            "source_story_word_count": word_count(source_text),
            "corrected_story_word_count": word_count(complete),
            "source_preservation_verification": source_verification,
        },
    }


def verify_revision_semantics(
    original: str,
    revised: str,
    complete_story: str,
    model: str,
    api_key: str,
) -> dict:
    prompt = f"""
You are an evidence-based Hindi story continuity auditor.

Compare ORIGINAL PART with REVISED PART.
Use COMPLETE STORY only as context.

IMPORTANT LANGUAGE RULES:
1. Treat Devanagari and Roman transliterations as equivalent when meaning is unchanged.
2. Common Hindi-English code-switching is not an event omission.
3. Spelling, matra, punctuation and grammar changes are not semantic changes.
4. Never report a missing event based only on different wording.
5. Do not invent omissions or additions.
6. Every alleged omission or addition must include exact textual evidence.

Return valid JSON only:
{{
  "all_events_preserved": true,
  "chronology_preserved": true,
  "characters_and_relationships_preserved": true,
  "locations_and_objects_preserved": true,
  "no_new_facts": true,
  "ending_and_consequences_preserved": true,
  "omitted_events": [],
  "added_events": [],
  "explanation": "..."
}}

All six booleans must be true and both arrays empty to pass.

COMPLETE STORY:
{complete_story}

ORIGINAL PART:
{original}

REVISED PART:
{revised}
"""

    result = call_model(
        prompt,
        model,
        api_key,
        max_tokens=3500,
    )

    if not isinstance(result, dict):
        raise RuntimeError("Story-preservation verifier returned a non-object response.")

    keys = (
        "all_events_preserved",
        "chronology_preserved",
        "characters_and_relationships_preserved",
        "locations_and_objects_preserved",
        "no_new_facts",
        "ending_and_consequences_preserved",
    )

    omissions = result.get("omitted_events", [])
    additions = result.get("added_events", [])

    if not isinstance(omissions, list):
        omissions = [str(omissions)]
    if not isinstance(additions, list):
        additions = [str(additions)]

    passed = (
        all(result.get(key) is True for key in keys)
        and not omissions
        and not additions
    )

    return {
        "performed": True,
        "passed": passed,
        "checks": {key: result.get(key) is True for key in keys},
        "omitted_events": omissions,
        "added_events": additions,
        "explanation": clean(result.get("explanation")),
        "model": (
            clean(result.get("_katha_model_used"))
            or get_selected_model()
        ),
    }


def repair_short_part(
    part_text: str,
    part_number: int,
    complete_story: str,
    title: str,
    model: str,
    api_key: str,
    *,
    final_merged: bool = False,
    return_evidence: bool = False,
):
    current = normalize_text(part_text)
    records: list[dict] = []
    original_hash = sha256(current)
    evidence = None

    hard_max = SHORT_MERGED_MAX if final_merged else SHORT_HARD_MAX

    for attempt in range(1, REVISION_ATTEMPTS + 1):
        count = word_count(current)

        if SHORT_MIN <= count <= SHORT_MAX:
            break

        direction = (
            "Compress toward 120–150 words without dropping events."
            if count > SHORT_MAX
            else
            "Expand toward 120–150 words using only facts already in the complete story."
        )

        prompt = f"""
Revise this Hindi narration Part.
Return JSON only: {{"text":"..."}}.

PART NUMBER: {part_number}
TITLE: {title}
CURRENT WORD COUNT: {count}
TARGET: 120–150 words.
NORMAL HARD LIMIT: 180 words.
FINAL MERGED PART LIMIT: 239 words, only when explicitly applicable.

DIRECTION: {direction}

COMPLETE STORY:
{complete_story}

CURRENT PART:
{current}

RULES:
- Preserve every event, character, relationship, chronology, clue,
  object and consequence.
- Do not invent facts, dialogue, clues, twists or outcomes.
- Do not omit important events or change the ending.
- Correct Hindi matras, spelling, grammar and punctuation.
- Keep complete sentence and meaningful clause boundaries.
- Preserve chronological order.
- Do not add a title, summary, heading or commentary.
"""

        record = {
            "attempt": attempt,
            "before_words": count,
            "before_sha256": sha256(current),
            "attempted": True,
        }

        try:
            response = call_model(
                prompt,
                model,
                api_key,
                max_tokens=5000,
            )

            revised = normalize_text(
                response.get("text")
                or response.get("story_text")
                or response.get("narration")
                or response.get("story")
            )

            if not revised:
                raise ModelError("Revision model returned empty narration.")

            revised_count = word_count(revised)
            record["after_words"] = revised_count
            record["after_sha256"] = sha256(revised)

            if sha256(revised) == sha256(current):
                record["outcome"] = "unchanged_text_no_revision_evidence"
                records.append(record)
                continue

            semantic = verify_revision_semantics(
                current,
                revised,
                complete_story,
                model,
                api_key,
            )
            record["semantic_verification"] = semantic

            if not semantic["passed"]:
                record["outcome"] = "semantic_preservation_failed"
                records.append(record)
                continue

            current = revised
            evidence = {
                "original_sha256": original_hash,
                "revised_sha256": sha256(current),
                "attempt_count": attempt,
                "returned_text_verified": True,
                "semantic_preservation_verified": True,
            }

            record["outcome"] = (
                "within_target_after_verified_revision"
                if SHORT_MIN <= revised_count <= SHORT_MAX
                else "verified_revision_outside_preferred_range"
            )
            records.append(record)

            if SHORT_MIN <= revised_count <= SHORT_MAX:
                break

        except Exception as exc:
            record["outcome"] = "failed"
            record["error"] = str(exc)
            records.append(record)

    final_count = word_count(current)

    if final_count < SHORT_MIN:
        raise RuntimeError(
            f"SHORT Part {part_number} remains below {SHORT_MIN} words "
            f"after {REVISION_ATTEMPTS} revision attempts ({final_count})."
        )

    if final_count > hard_max:
        raise RuntimeError(
            f"SHORT Part {part_number} exceeds its hard limit "
            f"of {hard_max} words ({final_count})."
        )

    if (
        final_count > SHORT_MAX
        and not verify_revision_evidence(evidence, current)
    ):
        raise RuntimeError(
            f"SHORT Part {part_number} has {final_count} words but "
            "has no verified changed-text revision evidence."
        )

    if return_evidence:
        return current, records, evidence

    return current, records


def _evidence_is_semantic(
    evidence: Any,
    text: str,
) -> bool:
    return bool(
        verify_revision_evidence(evidence, text)
        and isinstance(evidence, dict)
        and evidence.get("semantic_preservation_verified") is True
    )


def _rebalance_for_merged_count(
    story_text: str,
    part_count: int,
    scenes_range: tuple[int, int],
) -> list[str] | None:
    scenes_min, scenes_max = scenes_range
    units = split_sentences(story_text)

    if not units or part_count < 1:
        return None

    required = part_count * scenes_min

    if len(units) < required:
        try:
            units = _expand_to_scene_minimum(units, required)
        except RuntimeError:
            return None

    if not required <= len(units) <= part_count * scenes_max:
        return None

    groups = _balanced_unit_partition(
        units,
        part_count,
        scenes_min,
        scenes_max,
        allow_final_short=False,
        final_max=SHORT_MERGED_MAX,
    )

    if groups is None or len(groups) != part_count:
        return None

    counts = [word_count(group) for group in groups]

    if any(
        value < SHORT_MIN or value > SHORT_HARD_MAX
        for value in counts[:-1]
    ):
        return None

    if not SHORT_MIN <= counts[-1] <= SHORT_MERGED_MAX:
        return None

    return groups

def _partition_and_revise(
    complete_story: str,
    mode: str,
    parts_specification: str,
    story_length: Any,
    config: dict,
    model: str,
    api_key: str,
    title: str,
    scenes_range: tuple[int, int],
) -> tuple[list[str], dict, dict]:
    minimum_parts, maximum_parts = parse_parts_range(
        parts_specification
    )
    total_words = word_count(complete_story)

    if mode == "full":
        groups = [complete_story]
        merged_fallback = False
    else:
        preferred = select_final_part_count(
            total_corrected_words=total_words,
            format_type=mode,
            parts_specification=parts_specification,
            story_length=story_length,
            config=None,
        )
        groups, _selected_count, merged_fallback = _partition_short_story(
            complete_story,
            preferred,
            minimum_parts,
            maximum_parts,
            scenes_range,
        )

    revised_groups: list[str] = []
    revision_rows: list[dict] = []
    evidence_by_part: dict[int, dict] = {}
    attempted_parts: list[int] = []

    for number, original in enumerate(groups, 1):
        if mode == "full":
            revised_groups.append(original)
            revision_rows.append({
                "part": number,
                "attempted": False,
                "outcome": "full_unrestricted",
            })
            continue

        amount = word_count(original)
        is_final = number == len(groups)

        needs_revision = (
            amount < SHORT_MIN
            or (
                amount > SHORT_MAX
                and not (is_final and merged_fallback)
            )
        )

        if not needs_revision:
            revised_groups.append(original)
            revision_rows.append({
                "part": number,
                "attempted": False,
                "outcome": "already_within_target",
                "before_words": amount,
                "after_words": amount,
                "before_sha256": sha256(original),
                "after_sha256": sha256(original),
                "attempts": [],
            })
            continue

        try:
            revised, attempts, evidence = repair_short_part(
                original,
                number,
                complete_story,
                title,
                model,
                api_key,
                final_merged=bool(is_final and merged_fallback),
                return_evidence=True,
            )
        except RuntimeError as exc:
            # A final Part below 120 words must reach the explicit
            # retry-then-merge recovery below. repair_short_part raises
            # after exhausting retries, so preserve the untouched final
            # text here instead of aborting before that recovery can run.
            if not (is_final and amount < SHORT_MIN):
                raise
            revised = original
            attempts = [{
                "attempt": REVISION_ATTEMPTS,
                "attempted": True,
                "outcome": "final_part_retry_exhausted_merge_required",
                "error": str(exc),
                "before_words": amount,
                "after_words": amount,
                "before_sha256": sha256(original),
                "after_sha256": sha256(original),
            }]
            evidence = None

        revised_groups.append(revised)
        attempted_parts.append(number)

        if evidence:
            evidence_by_part[number] = evidence

        revision_rows.append({
            "part": number,
            "attempted": True,
            "outcome": (
                "verified_revision"
                if evidence
                else (
                    "final_part_retry_exhausted_merge_required"
                    if is_final and word_count(revised) < SHORT_MIN
                    else "retried_no_verified_change"
                )
            ),
            "before_words": amount,
            "after_words": word_count(revised),
            "before_sha256": sha256(original),
            "after_sha256": sha256(revised),
            "revision_evidence": evidence,
            "attempts": attempts,
            "final_part_merged": bool(is_final and merged_fallback),
        })

    # Final-Part exception is evaluated only after all Parts are revised.
    if mode == "short" and revised_groups:
        final_count = word_count(revised_groups[-1])

        if final_count < SHORT_MIN:
            if len(revised_groups) < 2:
                raise RuntimeError(
                    "The only SHORT Part remains below 120 words; "
                    "there is no preceding Part to merge into."
                )

            merged_text = (
                revised_groups[-2].rstrip()
                + "\n"
                + revised_groups[-1].lstrip()
            )
            merged_words = word_count(merged_text)

            if merged_words > SHORT_MERGED_MAX:
                target_count = len(revised_groups) - 1
                rebalance_source = "\n\n".join(revised_groups)
                rebalanced = _rebalance_for_merged_count(
                    rebalance_source,
                    target_count,
                    scenes_range,
                )

                if rebalanced is None:
                    raise RuntimeError(
                        f"Final-Part merge would contain {merged_words} words, "
                        f"above {SHORT_MERGED_MAX}; rebalancing into "
                        f"{target_count} Parts failed."
                    )

                revised_groups = rebalanced
                evidence_by_part = {}
                revision_rows = []
                attempted_parts = []
                merged_fallback = True

                for rebalance_number, rebalance_text in enumerate(
                    revised_groups,
                    1,
                ):
                    is_merged_final = (
                        rebalance_number == len(revised_groups)
                    )
                    amount = word_count(rebalance_text)

                    if amount < SHORT_MIN:
                        raise RuntimeError(
                            f"Rebalanced Part {rebalance_number} "
                            f"contains only {amount} words."
                        )

                    if amount > SHORT_MAX and not is_merged_final:
                        revised_text, retry_records, retry_evidence = (
                            repair_short_part(
                                rebalance_text,
                                rebalance_number,
                                rebalance_source,
                                title,
                                model,
                                api_key,
                                return_evidence=True,
                            )
                        )
                        revised_groups[rebalance_number - 1] = revised_text
                        attempted_parts.append(rebalance_number)

                        if retry_evidence:
                            evidence_by_part[rebalance_number] = retry_evidence

                        revision_rows.append({
                            "part": rebalance_number,
                            "attempted": True,
                            "outcome": (
                                "verified_after_final_rebalance"
                                if retry_evidence
                                else "unverified_after_final_rebalance"
                            ),
                            "before_words": amount,
                            "after_words": word_count(revised_text),
                            "before_sha256": sha256(rebalance_text),
                            "after_sha256": sha256(revised_text),
                            "revision_evidence": retry_evidence,
                            "attempts": retry_records,
                            "final_part_merged": is_merged_final,
                        })
                    else:
                        revision_rows.append({
                            "part": rebalance_number,
                            "attempted": False,
                            "outcome": "accepted_after_final_rebalance",
                            "before_words": amount,
                            "after_words": amount,
                            "before_sha256": sha256(rebalance_text),
                            "after_sha256": sha256(rebalance_text),
                            "attempts": [],
                            "final_part_merged": is_merged_final,
                        })

            else:
                previous_part_number = len(revised_groups) - 1
                revised_groups = revised_groups[:-2] + [merged_text]
                merged_fallback = True

                evidence_by_part.pop(previous_part_number, None)
                evidence_by_part.pop(previous_part_number + 1, None)

                revision_rows = [
                    row for row in revision_rows
                    if int(row.get("part", 0)) < previous_part_number
                ]
                attempted_parts = [
                    number for number in attempted_parts
                    if number < previous_part_number
                ]

                revision_rows.append({
                    "part": len(revised_groups),
                    "attempted": False,
                    "outcome": "final_short_part_merged_into_preceding_part",
                    "before_words": merged_words,
                    "after_words": merged_words,
                    "after_sha256": sha256(merged_text),
                    "final_part_merged": True,
                    "text_preserved_exactly": True,
                    "attempts": [],
                })

    if mode == "short":
        for number, part in enumerate(revised_groups, 1):
            amount = word_count(part)
            is_final_merged = bool(
                merged_fallback and number == len(revised_groups)
            )
            maximum = (
                SHORT_MERGED_MAX if is_final_merged
                else SHORT_HARD_MAX
            )

            if amount < SHORT_MIN or amount > maximum:
                raise RuntimeError(
                    f"SHORT Part {number} has {amount} words; "
                    f"allowed range is {SHORT_MIN}-{maximum}."
                )

            if amount > SHORT_MAX and not is_final_merged:
                if not _evidence_is_semantic(
                    evidence_by_part.get(number),
                    part,
                ):
                    raise RuntimeError(
                        f"SHORT Part {number} has {amount} words but "
                        "lacks verified revision and semantic evidence."
                    )

    final_story_text = "\n\n".join(revised_groups)

    if not final_story_text.strip():
        raise RuntimeError("Final story became empty after Part revision.")

    selected_count = len(revised_groups)

    set_selected_parts(
        config,
        selected_count,
        source="corrected_story_scene_aware_partition",
        story_word_count=word_count(final_story_text),
    )

    scenes_min, _scenes_max = scenes_range

    policy = build_length_policy(
        format_type=mode,
        story_length=story_length,
        parts=f"{minimum_parts}-{maximum_parts}",
        scenes_per_part=scenes_min,
        scene_duration=config.get("SCENE_DURATION", "auto"),
        source_word_count=word_count(final_story_text),
        selected_parts=selected_count,
    )

    policy.update({
        "corrected_story_word_count": word_count(final_story_text),
        "corrected_story_sha256": sha256(final_story_text),
        "story_was_corrected_before_counting": True,
        "selection_persisted": True,
        "selection_source": "corrected_story_scene_aware_partition",
        "revision_attempted": bool(attempted_parts),
        "revision_outcome": (
            "verified" if evidence_by_part
            else (
                "attempted_without_verified_change"
                if attempted_parts else "not_required"
            )
        ),
        "revision_parts": sorted(set(attempted_parts)),
        "revision_evidence_parts": sorted(evidence_by_part),
        "revision_results": revision_rows,
        "revision_attempt_count": sum(
            len(row.get("attempts", []))
            for row in revision_rows
        ),
        "final_part_merged": bool(merged_fallback),
        "final_merged_maximum_words": (
            SHORT_MERGED_MAX if merged_fallback else None
        ),
        "actual_parts": selected_count,
        "parts_count": selected_count,
        "selected_parts": selected_count,
        "final_parts_word_counts": [
            {
                "part": number,
                "words": word_count(part),
                "sha256": sha256(part),
                "revision_evidence": evidence_by_part.get(number),
            }
            for number, part in enumerate(revised_groups, 1)
        ],
    })

    validation_parts = [
        {
            "part": number,
            "narration": part,
            "revision_evidence": evidence_by_part.get(number),
            "final_part_merged": bool(
                merged_fallback and number == len(revised_groups)
            ),
        }
        for number, part in enumerate(revised_groups, 1)
    ]

    validation = validate_all_parts(validation_parts, policy)

    if mode == "short":
        errors = validation.get("errors", [])
        filtered_errors = [
            error for error in errors
            if not (
                merged_fallback
                and f"Part {len(revised_groups)}:" in str(error)
            )
        ]

        if filtered_errors:
            raise RuntimeError(
                "SHORT Part validation failed: "
                + " | ".join(map(str, filtered_errors))
            )

        validation["errors"] = []
        validation["valid"] = True
        validation["passed"] = True
        validation["final_validation_passed"] = True
        validation["final_merged_exception_applied"] = bool(
            merged_fallback
        )

        if merged_fallback:
            final_number = len(revised_groups)
            validation["retry_parts"] = [
                number
                for number in validation.get("retry_parts", [])
                if number != final_number
            ]
            validation["retry_required"] = bool(
                validation["retry_parts"]
            )
            validation["final_part_exception"] = None
            validation["final_part_merge_proposal"] = None

            for row in validation.get("parts", []):
                if row.get("part_number") == final_number:
                    row.update({
                        "status": "valid_final_merge_exception",
                        "needs_revision": False,
                        "retry_required": False,
                        "passed": True,
                        "final_validation_passed": True,
                        "merge_exception_applied": True,
                        "maximum_words": SHORT_MERGED_MAX,
                        "hard_maximum_words": SHORT_MERGED_MAX,
                    })

    # The initial policy is deliberately marked not-yet-validated.
    # Publish the result of the final validator only after all SHORT/FULL
    # checks (including the final-Part merge exception) have completed.
    final_validation_passed = bool(
        validation.get(
            "final_validation_passed",
            validation.get("passed", validation.get("valid", False)),
        )
    )
    if not final_validation_passed:
        raise RuntimeError(
            "Final story length validation did not pass; "
            "refusing to publish a completed story."
        )

    policy["final_part_validation_passed"] = True
    policy["parts_validation"] = {
        "valid": True,
        "actual_parts": selected_count,
        "word_counts": [word_count(part) for part in revised_groups],
        "final_part_merged": bool(merged_fallback),
        "final_merged_limit": (
            SHORT_MERGED_MAX if merged_fallback else None
        ),
        "policy_validator_report": validation,
    }

    selection = {
        "selected_parts": selected_count,
        "minimum_parts": minimum_parts,
        "maximum_parts": maximum_parts,
        "corrected_story_words": total_words,
        "final_story_words": word_count(final_story_text),
        "revision_attempted_parts": sorted(set(attempted_parts)),
        "final_part_merged": bool(merged_fallback),
    }

    return revised_groups, policy, selection


def select_final_parts(
    complete_story: str,
    mode: str,
    parts_specification: str,
    story_length: Any,
    config: dict,
    model: str,
    api_key: str,
    title: str,
) -> tuple[list[str], dict, dict]:
    return _partition_and_revise(
        complete_story=complete_story,
        mode=mode,
        parts_specification=parts_specification,
        story_length=story_length,
        config=config,
        model=model,
        api_key=api_key,
        title=title,
        scenes_range=get_scenes_range(config),
    )


def make_final_plan(
    config: dict,
    mode: str,
    topic: str,
    complete_story: str,
    parts_count: int,
    scenes_range: tuple[int, int],
    part_texts: list[str] | None = None,
) -> dict:
    if isinstance(scenes_range, int) and not isinstance(scenes_range, bool):
        scenes_range = (scenes_range, scenes_range)

    plan = build_story_plan(
        config=config,
        format_type=mode,
        topic=topic,
        story_text=complete_story,
        parts_count=parts_count,
        scenes_per_part=scenes_range,
        dynamic_short=False,
        part_texts=part_texts,
    )

    validate_story_plan(
        plan,
        mode,
        topic,
        parts_count,
        scenes_range,
    )

    source = plan.get("source")

    if (
        not isinstance(source, dict)
        or source.get("story_word_count") != word_count(complete_story)
    ):
        raise RuntimeError(
            "Final Story Plan was not built from the final corrected story."
        )

    if plan.get("parts_count") != parts_count:
        raise RuntimeError(
            "Final Story Plan has an incorrect Part count."
        )

    if plan.get("source_fingerprint") != fingerprint(
        plan.get("source_payload", {})
    ):
        raise RuntimeError(
            "Final Story Plan source fingerprint mismatch."
        )

    write_json(PLAN_FILE, plan)
    return plan


def _visual_prompt(
    scene_text: str,
    anchor: str,
    part_number: int,
    scene_number: int,
) -> str:
    return (
        "Photorealistic cinematic live-action frame depicting only "
        "the exact narrated story moment. "
        f"Part {part_number}, Scene {scene_number}. "
        f"Narration: {scene_text}. Story anchor: {anchor}. "
        "Use only characters, clothing, places, objects, actions and "
        "time details supported by the story. "
        "Maintain established identity, age, appearance, relationships, "
        "chronology and continuity. "
        "No unrelated stock imagery, invented events, text, collage "
        "or cartoon treatment."
    )


def make_story(
    title: str,
    topic: str,
    mode: str,
    source_text: str,
    complete_story: str,
    parts: list[str],
    plan: dict,
    scenes_range: tuple[int, int],
    model: str,
    proofreading: dict,
    policy: dict,
    selection: dict,
    source_fingerprint: str,
) -> dict:
    if isinstance(scenes_range, int) and not isinstance(scenes_range, bool):
        scenes_range = (scenes_range, scenes_range)

    plan_parts = plan.get("parts")

    if not isinstance(plan_parts, list) or len(plan_parts) != len(parts):
        raise RuntimeError(
            "Final plan and final story Part counts do not match."
        )

    scenes_min, scenes_max = scenes_range

    story_parts: list[dict] = []
    all_scenes: list[dict] = []
    all_hooks: list[dict] = []
    all_suspense: list[dict] = []
    all_resolutions: list[dict] = []
    global_scene = 0

    for part_number, original_narration in enumerate(parts, 1):
        plan_part = plan_parts[part_number - 1]
        planned_scenes = (
            plan_part.get("scenes")
            if isinstance(plan_part, dict)
            else None
        )

        if (
            not isinstance(planned_scenes, list)
            or not scenes_min <= len(planned_scenes) <= scenes_max
        ):
            raise RuntimeError(
                f"Plan Scene count is outside {scenes_min}-{scenes_max} "
                f"for Part {part_number}."
            )

        narration = normalize_text(plan_part.get("narration"))

        if clean(narration) != clean(original_narration):
            raise RuntimeError(
                f"Planner changed Part {part_number} narration "
                "after final length validation."
            )

        scenes = []

        for scene_number, planned in enumerate(planned_scenes, 1):
            if not isinstance(planned, dict):
                raise RuntimeError(
                    "Invalid final Story Plan Scene record."
                )

            global_scene += 1
            expected_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            if planned.get("scene_id") != expected_id:
                raise RuntimeError(
                    f"Story Plan Scene ID mismatch: {expected_id}."
                )

            scene_text = normalize_text(
                planned.get("narration")
                or planned.get("story_anchor")
            )
            anchor = clean(planned.get("story_anchor"))

            if not scene_text or clean(scene_text) != anchor:
                raise RuntimeError(
                    f"Story Plan Scene narration/anchor mismatch: {expected_id}."
                )

            visual = clean(
                planned.get("visual_prompt")
                or planned.get("visual")
                or planned.get("description")
            )

            if not visual:
                visual = _visual_prompt(
                    scene_text,
                    anchor,
                    part_number,
                    scene_number,
                )

            scene = {
                "part": part_number,
                "scene": scene_number,
                "scene_id": expected_id,
                "global_scene_number": global_scene,
                "role": "main",
                "beat": clean(planned.get("beat")),
                "purpose": clean(planned.get("purpose")),
                "story_anchor": anchor,
                "narration": scene_text,
                "visual": visual,
                "visual_prompt": visual,
                "dialogue": "",
                "suspense": clean(planned.get("purpose")),
            }

            scenes.append(scene)
            all_scenes.append(scene)

        is_final = part_number == len(plan_parts)
        hook = plan_part.get("hook_scene")
        suspense = plan_part.get("suspense_scene")
        resolution = plan_part.get("resolution_scene")

        if not isinstance(hook, dict) or hook.get("role") != "hook":
            raise RuntimeError(
                f"Part {part_number} has no separate Hook record."
            )

        if is_final and (
            not isinstance(resolution, dict)
            or resolution.get("role") != "resolution"
        ):
            raise RuntimeError(
                "Final Part has no separate Resolution record."
            )

        if not is_final and (
            not isinstance(suspense, dict)
            or suspense.get("role") != "suspense"
        ):
            raise RuntimeError(
                f"Non-final Part {part_number} has no separate Suspense record."
            )

        all_hooks.append(hook)

        if suspense:
            all_suspense.append(suspense)

        if resolution:
            all_resolutions.append(resolution)

        extras = [
            item for item in (hook, suspense, resolution)
            if isinstance(item, dict)
        ]

        revision_rows = policy.get("revision_results", [])
        narration_hash = sha256(narration)

        revision_row = next(
            (
                row for row in reversed(revision_rows)
                if row.get("part") == part_number
                and row.get("after_sha256") == narration_hash
            ),
            {},
        )

        evidence = revision_row.get("revision_evidence")

        story_parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": f"भाग {part_number}",
            "narration": narration,
            "story_anchor": narration,
            "word_count": word_count(narration),
            "revision_attempted": bool(evidence),
            "revision_evidence": evidence,
            "final_part_merged": bool(
                selection.get("final_part_merged") and is_final
            ),
            "main_scene_count": len(scenes),
            "actual_scene_count": len(scenes),
            "hook_scene": hook,
            "suspense_scene": suspense,
            "resolution_scene": resolution,
            "extra_scenes": extras,
            "scenes": scenes,
        })

    final_story_text = "\n\n".join(
        row["narration"] for row in story_parts
    )

    if clean(final_story_text) != clean(complete_story):
        raise RuntimeError(
            "Final Part narrations do not reconstruct corrected_complete_story."
        )

    actual_counts = [
        part["main_scene_count"] for part in story_parts
    ]

    if len(all_scenes) != sum(actual_counts):
        raise RuntimeError(
            "Actual Main Scene counts do not match the generated Scene list."
        )

    if not isinstance(policy, dict):
        policy = {}

    policy.update({
        "actual_parts": len(story_parts),
        "parts_count": len(story_parts),
        "selected_parts": len(story_parts),
        "actual_scene_counts": list(actual_counts),
        "total_scenes": len(all_scenes),
        "total_main_scenes": len(all_scenes),
    })

    write_json(POLICY_FILE, policy)

    if len(all_scenes) > MAX_TOTAL_SCENES:
        raise RuntimeError(
            f"Final plan contains {len(all_scenes)} Main Scenes; "
            f"limit is {MAX_TOTAL_SCENES}."
        )

    result = {
        "version": VERSION,
        "status": "completed",
        "title": title,
        "final_title": title,
        "topic": topic,
        "format": mode,
        "model": model,
        "generation_method": (
            clean(proofreading.get("generation_method"))
            or "configured_gemini"
        ),
        "proofreading": proofreading,
        "source_text": source_text,
        "source_word_count": word_count(source_text),
        "corrected_complete_story": final_story_text,
        "corrected_complete_story_word_count": word_count(final_story_text),
        "story_text": final_story_text,
        "total_story_words": word_count(final_story_text),
        "total_parts": len(story_parts),
        "parts_count": len(story_parts),
        "selected_parts": len(story_parts),
        "selected_parts_count": len(story_parts),
        "configured_parts_specification": (
            f"{selection['minimum_parts']}-{selection['maximum_parts']}"
            if selection["minimum_parts"] != selection["maximum_parts"]
            else str(selection["minimum_parts"])
        ),
        "configured_parts_min": selection["minimum_parts"],
        "configured_parts_max": selection["maximum_parts"],
        "scenes_per_part": scenes_range[0],
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": actual_counts,
        "total_scenes": len(all_scenes),
        "total_main_scenes": len(all_scenes),
        "total_hook_scenes": len(all_hooks),
        "total_suspense_scenes": len(all_suspense),
        "total_resolution_scenes": len(all_resolutions),
        "scene_order": "part_then_scene",
        "role_order": "hook_then_main_then_suspense_or_resolution",
        "parts": story_parts,
        "scenes": all_scenes,
        "hook_scenes": all_hooks,
        "suspense_scenes": all_suspense,
        "resolution_scenes": all_resolutions,
        "story_plan_fingerprint": plan.get("source_fingerprint"),
        "story_length_policy": policy,
        "parts_selection": selection,
        "source_fingerprint": source_fingerprint,
    }

    validate_story(result, len(story_parts), scenes_range, mode)
    return result


def validate_story(
    story: dict,
    expected_parts: int,
    scenes_range: tuple[int, int],
    mode: str,
) -> None:
    if not isinstance(story, dict):
        raise RuntimeError("Story output must be a JSON object.")

    parts = story.get("parts")
    scenes = story.get("scenes")

    if not isinstance(parts, list) or len(parts) != expected_parts:
        raise RuntimeError("Story Part count validation failed.")

    if not isinstance(scenes, list):
        raise RuntimeError("Story Main Scenes must be a list.")

    scenes_min, scenes_max = scenes_range
    reconstructed = []

    for number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid Part {number} record.")

        narration = normalize_text(part.get("narration"))

        if not narration:
            raise RuntimeError(f"Part {number} narration is empty.")

        reconstructed.append(narration)
        part_scenes = part.get("scenes")

        if (
            not isinstance(part_scenes, list)
            or not scenes_min <= len(part_scenes) <= scenes_max
        ):
            raise RuntimeError(
                f"Part {number} Main Scene count is outside "
                f"{scenes_min}-{scenes_max}."
            )

        if part.get("main_scene_count") != len(part_scenes):
            raise RuntimeError(
                f"Part {number} Main Scene count metadata mismatch."
            )

        if part.get("word_count") != word_count(narration):
            raise RuntimeError(
                f"Part {number} word count metadata mismatch."
            )

        if not isinstance(part.get("hook_scene"), dict):
            raise RuntimeError(f"Part {number} Hook Scene is missing.")

        if number < expected_parts:
            if not isinstance(part.get("suspense_scene"), dict):
                raise RuntimeError(
                    f"Part {number} Suspense Scene is missing."
                )
        elif not isinstance(part.get("resolution_scene"), dict):
            raise RuntimeError(
                "Final Part Resolution Scene is missing."
            )

    reconstructed_story = "\n\n".join(reconstructed)

    if clean(reconstructed_story) != clean(
        story.get("corrected_complete_story", "")
    ):
        raise RuntimeError(
            "Part narration does not reconstruct the complete story."
        )

    if len(scenes) != sum(len(part["scenes"]) for part in parts):
        raise RuntimeError("Global Main Scene count mismatch.")

    if story.get("total_parts") != expected_parts:
        raise RuntimeError("Total Part metadata mismatch.")

    if story.get("total_scenes") != len(scenes):
        raise RuntimeError("Total Scene metadata mismatch.")

    if story.get("actual_scene_counts") != [
        len(part["scenes"]) for part in parts
    ]:
        raise RuntimeError("Actual Scene count metadata mismatch.")

    selection = story.get("parts_selection")
    if not isinstance(selection, dict):
        raise RuntimeError("Story Part-selection metadata is missing.")

    minimum_parts = selection.get("minimum_parts")
    maximum_parts = selection.get("maximum_parts")
    if (
        isinstance(minimum_parts, bool)
        or not isinstance(minimum_parts, int)
        or isinstance(maximum_parts, bool)
        or not isinstance(maximum_parts, int)
        or minimum_parts < 1
        or maximum_parts < minimum_parts
        or not minimum_parts <= expected_parts <= maximum_parts
    ):
        raise RuntimeError("Story selected Part count is outside its configured range.")

    if story.get("parts_count") != expected_parts:
        raise RuntimeError("Story Parts-count metadata mismatch.")

    if story.get("total_story_words") != word_count(reconstructed_story):
        raise RuntimeError("Total story word-count metadata mismatch.")

    for number, part in enumerate(parts, 1):
        if part.get("part") != number or part.get("part_id") != f"part_{number:02d}":
            raise RuntimeError(f"Part {number} ID/order metadata mismatch.")

    if mode == "short":
        merged = bool(selection.get("final_part_merged"))

        for number, part in enumerate(parts, 1):
            count = word_count(part["narration"])
            maximum = (
                SHORT_MERGED_MAX
                if merged and number == expected_parts
                else SHORT_HARD_MAX
            )

            if count < SHORT_MIN or count > maximum:
                raise RuntimeError(
                    f"SHORT Part {number} has {count} words; "
                    f"allowed {SHORT_MIN}-{maximum}."
                )


def cache_is_valid(
    cached: Any,
    source_fingerprint: str,
    mode: str,
    scenes_range: tuple[int, int],
) -> bool:
    if not isinstance(cached, dict):
        return False

    try:
        if cached.get("status") != "completed":
            return False

        if cached.get("version") != VERSION:
            return False

        if cached.get("source_fingerprint") != source_fingerprint:
            return False

        selection = cached.get("parts_selection")
        policy = cached.get("story_length_policy")
        if not isinstance(selection, dict) or not isinstance(policy, dict):
            return False
        if not isinstance(selection.get("minimum_parts"), int):
            return False
        if not isinstance(selection.get("maximum_parts"), int):
            return False
        if selection["minimum_parts"] < 1 or selection["maximum_parts"] < selection["minimum_parts"]:
            return False

        parts = cached.get("parts")

        if not isinstance(parts, list) or not parts:
            return False

        validate_story(cached, len(parts), scenes_range, mode)
        return True

    except Exception as exc:
        log(f"Cache rejected: {exc}")
        return False


def reusable_corrected_story_checkpoint(
    source_fingerprint: str,
) -> dict | None:
    candidates = [
        CACHE_FILE,
        CHECKPOINT_FILE,
    ]

    for path in candidates:
        if not path.is_file():
            continue

        try:
            data = read_json(path)

            if not isinstance(data, dict):
                continue

            corrected = normalize_text(
                data.get("corrected_complete_story")
                or data.get("corrected_story")
                or data.get("story_text")
            )

            if not corrected:
                continue

            stored_fingerprint = data.get("source_fingerprint")

            # A checkpoint without an exact source fingerprint cannot be
            # proven to belong to the current input and is never reusable.
            if stored_fingerprint != source_fingerprint:
                continue

            log(
                f"Reusable corrected-story checkpoint found: "
                f"{os.path.basename(path)}"
            )

            return {
                "path": path,
                "data": data,
                "corrected_story": corrected,
                "reused": True,
            }

        except Exception as exc:
            log(
                f"Checkpoint ignored ({os.path.basename(path)}): {exc}"
            )

    return None


def get_final_title(
    model: str,
    api_key: str,
    topic: str,
    complete_story: str,
    existing_title: str = "",
) -> str:
    existing_title = clean(existing_title)

    if existing_title:
        return existing_title[:180]

    prompt = f"""
Create one concise, compelling Hindi story title based on the
ENTIRE story below.

Do not base the title only on its opening.
Do not add events or characters.
Return JSON only in this format:
{{"title":"..."}}.

Story:
{complete_story}
"""

    response = call_model(
        prompt,
        model,
        api_key,
        max_tokens=120,
    )

    title = clean(
        response.get("title")
        or response.get("story_text")
        or response.get("text")
    )

    if not title:
        title = clean(topic)

    if not title:
        raise RuntimeError("Could not determine a story title.")

    return title[:180]


def load_persisted_title(complete_story: str) -> str:
    """Reuse a title only when its saved story fingerprint matches exactly."""
    data = read_json(TITLE_CACHE_FILE)
    if not isinstance(data, dict):
        return ""

    expected_fingerprint = sha256(normalize_text(complete_story))
    if data.get("story_fingerprint") != expected_fingerprint:
        return ""

    return clean(data.get("title"))[:180]


def save_persisted_title(title: str, complete_story: str) -> None:
    """Persist the final title against the complete corrected story."""
    title = clean(title)[:180]
    if not title:
        raise RuntimeError("Refusing to persist an empty story title.")

    write_json(
        TITLE_CACHE_FILE,
        {
            "version": 1,
            "story_fingerprint": sha256(normalize_text(complete_story)),
            "title": title,
        },
    )


def main() -> None:
    STORY_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    config = load_and_validate()

    mode = get_format(config).strip().lower()

    if mode not in ("short", "full"):
        raise RuntimeError(
            f"Unsupported video format: {mode!r}"
        )

    topic = clean(get_topic(config))
    explicit_title = clean(config.get("TITLE", ""))
    title_source = explicit_title or clean(get_title_source(config))
    source_text = normalize_text(
        get_complete_source(config)
    )

    if not source_text and not topic and not title_source:
        raise RuntimeError(
            "Provide a story, title or topic in Input/topic.txt."
        )

    scenes_range = get_scenes_range(config)

    if isinstance(scenes_range, int):
        scenes_range = (scenes_range, scenes_range)

    if (
        not isinstance(scenes_range, (tuple, list))
        or len(scenes_range) != 2
    ):
        raise RuntimeError("Invalid SCENES range.")

    scenes_range = (
        int(scenes_range[0]),
        int(scenes_range[1]),
    )

    if (
        scenes_range[0] < 1
        or scenes_range[1] < scenes_range[0]
        or scenes_range[1] > 100
    ):
        raise RuntimeError(
            f"Invalid SCENES range: {scenes_range}."
        )

    parts_min, parts_max = get_parts_range(config)

    parts_specification = (
        str(parts_min)
        if parts_min == parts_max
        else f"{parts_min}-{parts_max}"
    )

    source_fingerprint = fingerprint({
        "mode": mode,
        "topic": topic,
        "title_source": title_source,
        "source_text": source_text,
        "parts": parts_specification,
        "scenes_range": list(scenes_range),
    })

    model = get_selected_model()
    api_key = get_api_key()

    cached = read_json(CACHE_FILE)

    if cache_is_valid(
        cached,
        source_fingerprint,
        mode,
        scenes_range,
    ):
        log(
            "Valid completed Story cache found; "
            "reusing corrected story and existing Parts."
        )

        corrected_story = normalize_text(
            cached["corrected_complete_story"]
        )

        parts = [
            normalize_text(part["narration"])
            for part in cached["parts"]
        ]

        cached_selection = cached["parts_selection"]
        cached_minimum = int(cached_selection["minimum_parts"])
        cached_maximum = int(cached_selection["maximum_parts"])
        if not cached_minimum <= len(parts) <= cached_maximum:
            raise RuntimeError(
                "Cached selected Part count is outside the current configured range."
            )
        persisted_parts = set_selected_parts(
            config,
            len(parts),
            source="validated_story_cache_reuse",
            story_word_count=word_count(cached["corrected_complete_story"]),
        )
        if persisted_parts != len(parts):
            raise RuntimeError(
                "Could not persist the validated cached Part selection."
            )

        title = clean(
            explicit_title
            or cached.get("final_title")
            or cached.get("title")
            or load_persisted_title(corrected_story)
        )

        if not title:
            title = get_final_title(
                model,
                api_key,
                topic,
                corrected_story,
            )

        save_persisted_title(title, corrected_story)

        model = clean(cached.get("model")) or model

        policy = cached.get("story_length_policy", {})
        selection = cached.get("parts_selection", {})
        proofreading = cached.get("proofreading", {})

        plan = make_final_plan(
            config=config,
            mode=mode,
            topic=topic,
            complete_story=corrected_story,
            parts_count=len(parts),
            scenes_range=scenes_range,
            part_texts=parts,
        )

        rebuilt_story = make_story(
            title=title,
            topic=topic,
            mode=mode,
            source_text=source_text,
            complete_story=corrected_story,
            parts=parts,
            plan=plan,
            scenes_range=scenes_range,
            model=model,
            proofreading=proofreading,
            policy=policy,
            selection=selection,
            source_fingerprint=source_fingerprint,
        )

        write_json(OUTPUT_FILE, rebuilt_story)
        write_json(CACHE_FILE, rebuilt_story)

        atomic_write(
            TITLE_FILE,
            title + "\n",
        )

        log(
            f"Completed from cache: {len(parts)} Parts, "
            f"{len(rebuilt_story['scenes'])} Main Scenes."
        )
        return

    checkpoint = reusable_corrected_story_checkpoint(
        source_fingerprint
    )

    checkpoint_data = {}

    if checkpoint:
        checkpoint_data = checkpoint["data"]
        complete_story = checkpoint["corrected_story"]

        proofreading = checkpoint_data.get(
            "proofreading",
            {
                "reused_checkpoint": True,
                "source_fingerprint": source_fingerprint,
            },
        )

        log(
            "Reusing corrected-story checkpoint; "
            "skipping story generation."
        )

    else:
        generated = generate_complete_story(
            mode=mode,
            topic=topic,
            title=title_source,
            source_text=source_text,
            config=config,
            model=model,
            api_key=api_key,
        )

        if not isinstance(generated, dict):
            raise RuntimeError(
                "Story generator returned an invalid result."
            )

        complete_story = normalize_text(
            generated.get("story_text")
        )

        proofreading = generated.get(
            "proofreading",
            {},
        )

        model = clean(generated.get("model")) or model

        generated_title = clean(
            generated.get("title")
        )

        if generated_title and not explicit_title:
            title_source = generated_title

    complete_story = normalize_text(complete_story)

    if not complete_story:
        raise RuntimeError(
            "Corrected complete story is empty."
        )

    title = get_final_title(
        model=model,
        api_key=api_key,
        topic=topic,
        complete_story=complete_story,
        existing_title=(
            explicit_title
            or generated_title
            or checkpoint_data.get("final_title")
            or checkpoint_data.get("title")
            or load_persisted_title(complete_story)
        ),
    )

    save_persisted_title(title, complete_story)

    model = clean(proofreading.get("model")) or model

    atomic_write(
        CORRECTED_STORY_FILE,
        complete_story + "\n",
    )

    write_json(
        CHECKPOINT_FILE,
        {
            "status": "corrected_story_ready",
            "source_fingerprint": source_fingerprint,
            "corrected_complete_story": complete_story,
            "corrected_complete_story_word_count": word_count(
                complete_story
            ),
            "final_title": title,
            "proofreading": proofreading,
            "model": model,
            "reused_checkpoint": bool(checkpoint),
        },
    )

    parts, policy, selection = select_final_parts(
        complete_story=complete_story,
        mode=mode,
        parts_specification=parts_specification,
        story_length=config.get("STORY_LENGTH", "auto"),
        config=config,
        model=model,
        api_key=api_key,
        title=title,
    )

    final_story_text = "\n\n".join(parts)

    if not final_story_text.strip():
        raise RuntimeError(
            "Final Part segmentation returned empty text."
        )

    atomic_write(
        CORRECTED_STORY_FILE,
        final_story_text + "\n",
    )

    write_json(
        POLICY_FILE,
        policy,
    )

    plan = make_final_plan(
        config=config,
        mode=mode,
        topic=topic,
        complete_story=final_story_text,
        parts_count=len(parts),
        scenes_range=scenes_range,
        part_texts=parts,
    )

    result = make_story(
        title=title,
        topic=topic,
        mode=mode,
        source_text=source_text,
        complete_story=final_story_text,
        parts=parts,
        plan=plan,
        scenes_range=scenes_range,
        model=model,
        proofreading=proofreading,
        policy=policy,
        selection=selection,
        source_fingerprint=source_fingerprint,
    )

    validate_story(
        result,
        len(parts),
        scenes_range,
        mode,
    )

    write_json(
        OUTPUT_FILE,
        result,
    )

    write_json(
        CACHE_FILE,
        result,
    )

    atomic_write(
        TITLE_FILE,
        title + "\n",
    )

    log(
        f"Story generation completed: {len(parts)} Parts, "
        f"{len(result['scenes'])} Main Scenes."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"FATAL: {type(exc).__name__}: {exc}")
        raise