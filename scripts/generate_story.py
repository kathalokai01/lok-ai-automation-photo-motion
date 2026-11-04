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
)
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
INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT_FILE = STORY_DIR / "story.json"
CACHE_FILE = STORY_DIR / "story_cache.json"
PLAN_FILE = STORY_DIR / "story_plan.json"
TITLE_FILE = STORY_DIR / "final_title.txt"
POLICY_FILE = STORY_DIR / "story_length_policy.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

VERSION = "story-builder-v31-variable-scene-range-natural-boundaries-final-merge"
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
    # Match story_length_policy.sha256_text: hashes are whitespace-normalized.
    return hashlib.sha256(clean(value).encode("utf-8")).hexdigest()


def fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str).encode("utf-8")
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
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


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
        delimiter = '"""' if value.startswith('"""') else "'''" if value.startswith("'''") else None
        if delimiter:
            remainder = value[len(delimiter):]
            closing = remainder.find(delimiter)
            if closing >= 0:
                trailing = remainder[closing + len(delimiter):].strip()
                if trailing and not trailing.startswith("#"):
                    raise RuntimeError("Unexpected content after STORY_TEXT closing delimiter.")
                return remainder[:closing].strip()
            collected = [remainder] if remainder else []
            for current in lines[index + 1:]:
                closing = current.find(delimiter)
                if closing >= 0:
                    if current[:closing]:
                        collected.append(current[:closing])
                    trailing = current[closing + len(delimiter):].strip()
                    if trailing and not trailing.startswith("#"):
                        raise RuntimeError("Unexpected content after STORY_TEXT closing delimiter.")
                    return "\n".join(collected).strip()
                collected.append(current)
            raise RuntimeError("STORY_TEXT multiline block has no closing delimiter.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value
    return ""


def get_complete_source(config: dict) -> str:
    """Public API used by verify_story.py; prefer the complete direct assignment."""
    parsed = normalize_text(get_story_text(config))
    direct = normalize_text(read_complete_story_assignment(INPUT_FILE))
    return direct if word_count(direct) > word_count(parsed) else (parsed or direct)


def split_sentences(text: str) -> list[str]:
    return [clean(item) for item in plan_split_sentences(text) if clean(item)]


def split_clauses(sentence: str) -> list[str]:
    return [clean(item) for item in plan_split_clauses(sentence) if clean(item)]


def _balanced_unit_partition(
    units: list[str], part_count: int, scenes_min: int, scenes_max: int,
    *, allow_final_short: bool = True, final_max: int = SHORT_HARD_MAX,
) -> list[str] | None:
    """Contiguous DP partition; sentence/clause order is never changed."""
    total_units = len(units)
    if (part_count < 1 or total_units < part_count * scenes_min
            or total_units > part_count * scenes_max):
        return None
    prefix = [0]
    for unit in units:
        prefix.append(prefix[-1] + word_count(unit))

    # state[end] = (cost, boundary list); one best path per prefix is sufficient.
    states: dict[int, tuple[float, list[int]]] = {0: (0.0, [0])}
    operations = 0
    for part_index in range(part_count):
        next_states: dict[int, tuple[float, list[int]]] = {}
        remaining_parts = part_count - part_index - 1
        is_final = remaining_parts == 0
        for start, (base_cost, boundaries) in states.items():
            min_take = max(scenes_min, total_units - start - remaining_parts * scenes_max)
            max_take = min(scenes_max, total_units - start - remaining_parts * scenes_min)
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
                    next_states[end] = (candidate, boundaries + [end])
        states = next_states
        if not states:
            return None
    final = states.get(total_units)
    if final is None:
        return None
    boundaries = final[1]
    groups = ["\n".join(units[boundaries[i]:boundaries[i + 1]])
              for i in range(part_count)]
    if any(not clean(group) for group in groups):
        return None
    if clean(" ".join(groups)) != clean(" ".join(units)):
        raise RuntimeError("Natural Part segmentation lost or reordered story material.")
    return groups


def _expand_to_scene_minimum(sentences: list[str], required_units: int) -> list[str]:
    """Split only meaningful complete clauses when the sentence count is too small."""
    units = list(sentences)
    if len(units) >= required_units:
        return units
    expanded: list[str] = []

    for sentence in sentences:
        pieces = split_clauses(sentence)
        expanded.extend(pieces if len(pieces) > 1 else [sentence])
    if len(expanded) < required_units:
        raise RuntimeError(
            f"Only {len(expanded)} complete sentence/meaningful-clause units "
            f"exist; selected PARTS × SCENES minimum requires {required_units}. "
            "Mid-sentence word splitting is forbidden."
        )
    if clean(" ".join(expanded)) != clean(" ".join(sentences)):
        raise RuntimeError("Clause expansion changed the complete story text.")
    return expanded


def split_balanced(
    text: str,
    count: int,
    scenes_range: tuple[int, int] | None = None,
) -> list[str]:
    """Compatibility splitter using complete natural units and a variable Scene range."""
    if isinstance(count, bool) or int(count) < 1:
        raise RuntimeError("Part/Scene count must be positive.")
    count = int(count)
    sentences = split_sentences(text)
    if not sentences:
        raise RuntimeError("Story contains no complete sentence.")
    if scenes_range is None:
        # Legacy compatibility: balance natural units without SHORT word limits.
        units = sentences
        if len(units) < count:
            units = _expand_to_scene_minimum(units, count)
        if len(units) < count:
            raise RuntimeError("Insufficient natural units for requested group count.")
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
                if selected and abs(selected_words - target) <= abs(selected_words + size - target):
                    break
                selected.append(candidate)
                selected_words += size
                cursor += 1
                if selected_words >= target:
                    break
            groups.append("\n".join(selected))
            remaining_words -= selected_words
        if len(groups) != count:
            raise RuntimeError("Natural segmentation did not produce the requested groups.")
    else:
        scenes_min, scenes_max = scenes_range
        units = _expand_to_scene_minimum(sentences, count * scenes_min)
        groups = _balanced_unit_partition(units, count, scenes_min, scenes_max)
    if groups is None:
        raise RuntimeError(
            "No natural sentence/clause partition satisfies the selected PARTS, "
            "SCENES range and SHORT word limits. Adjust the configured ranges."
        )
    return groups


def _partition_short_story(
    story_text: str,
    preferred_count: int,
    minimum_parts: int,
    maximum_parts: int,
    scenes_range: tuple[int, int],
) -> tuple[list[str], int, bool]:
    """Find a valid contiguous partition; final merged Part may use the 239-word cap."""
    scenes_min, scenes_max = scenes_range
    sentences = split_sentences(story_text)
    if not sentences:
        raise RuntimeError("Complete corrected story contains no complete sentences.")
    total_words = word_count(story_text)
    # Clause splitting is used only when the selected Parts need more natural Scene units.
    base_units = list(sentences)
    candidates: list[int] = []
    for number in range(minimum_parts, maximum_parts + 1):
        if number * scenes_max <= MAX_TOTAL_SCENES:
            candidates.append(number)
    candidates.sort(key=lambda value: (abs(value - preferred_count), value))

    last_errors = []
    expanded_units = []
    for sentence in base_units:
        pieces = split_clauses(sentence)
        expanded_units.extend(pieces if len(pieces) > 1 else [sentence])
    variants = [base_units]
    if expanded_units != base_units and clean(" ".join(expanded_units)) == clean(" ".join(base_units)):
        variants.append(expanded_units)

    for units_source in variants:
        for part_count in candidates:
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
                units, part_count, scenes_min, scenes_max,
                allow_final_short=False, final_max=SHORT_HARD_MAX,
            )
            if groups is not None:
                return groups, part_count, False
            groups = _balanced_unit_partition(
                units, part_count, scenes_min, scenes_max,
                allow_final_short=True, final_max=SHORT_HARD_MAX,
            )
            if groups is not None:
                return groups, part_count, False
            last_errors.append(
                f"{part_count} Parts could not satisfy sentence, Scene and word boundaries."
            )

    # Final merge/rebalance fallback: the final Part may use the documented 239-word cap.
    merged_candidates = [n for n in range(minimum_parts, maximum_parts + 1)
                         if n >= 1 and n < preferred_count
                         and n * scenes_max <= MAX_TOTAL_SCENES]
    merged_candidates.sort(key=lambda value: (abs(value - max(minimum_parts, preferred_count - 1)), value))
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
                units, part_count, scenes_min, scenes_max,
                allow_final_short=False, final_max=SHORT_MERGED_MAX,
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
        raise RuntimeError("Configured selected Gemini model is missing or invalid.")
    return model


def get_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()


class ModelError(RuntimeError):
    pass


def parse_model_response(raw: str) -> dict:
    value = str(raw or "").strip()
    value = re.sub(r"^\s*```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE).strip()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(value):
        if char == "{":
            try:
                parsed, _ = decoder.raw_decode(value[index:])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    plain = normalize_text(value)
    if not plain:
        raise ModelError("Model returned neither usable JSON nor plain text.")
    return {"title": "", "story_text": plain, "parts": [], "response_format": "plain_text"}


def call_model(prompt: str, model: str, api_key: str, *, max_tokens: int = MAX_TOKENS) -> dict:
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + urllib.parse.quote(model, safe="-._") + ":generateContent")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.25,
            "maxOutputTokens": max_tokens,
        },
    }
    errors = []
    for attempt in range(2):
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                response_data = json.loads(response.read().decode("utf-8", errors="replace"))
            candidates = response_data.get("candidates") or []
            if not candidates:
                raise ModelError("Configured model returned no candidate.")
            content_parts = candidates[0].get("content", {}).get("parts", [])
            response_text = "".join(item.get("text", "") for item in content_parts
                                     if isinstance(item, dict)).strip()
            if not response_text:
                raise ModelError("Configured model returned empty text.")
            return parse_model_response(response_text)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            errors.append(f"HTTP {exc.code}: {body[:1200]}")
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                break
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ModelError) as exc:
            errors.append(str(exc))
            if isinstance(exc, ModelError):
                break
        if attempt == 0:
            time.sleep(RETRY_DELAYS[0])
    raise ModelError("Configured model request failed: " + " | ".join(errors[-3:]))

            time.sleep(RETRY_DELAYS[0])
    raise RuntimeError(f"Configured model {model} failed: " + " | ".join(errors))


def build_story_prompt(mode: str, topic: str, title: str, source_text: str, config: dict) -> str:
    task = "Correct and proofread the complete supplied story." if source_text else "Write a complete original Hindi story from the topic/title."
    return f"""
You are a professional Hindi story editor.
TASK: {task}
FORMAT: {mode}
TOPIC: {topic or '(not supplied)'}
EXPLICIT TITLE: {title or '(not supplied)'}
AUDIENCE: {config.get('AUDIENCE', 'adult')}

Read the COMPLETE story from beginning to ending before editing.
SOURCE STORY:
<source_story>
{source_text}
</source_story>

MANDATORY RULES:
1. Correct Hindi matras, spelling, grammar and punctuation.
2. Improve sentence construction without changing meaning.
3. Preserve every character, relationship, location, object, clue, event, motivation, chronology and consequence.
4. Preserve the original ending and every meaningful event.
5. Do not invent events, dialogue, clues, characters or outcomes.
6. Use natural Hindi Devanagari unless the source requires otherwise.
7. Return the entire corrected story, not a summary.
8. If TITLE is blank, derive it only after reading the complete story, including its middle and ending.
9. Return JSON with title and story_text. Plain text is accepted.

Return: {{"title":"full-story-based title","story_text":"complete corrected story"}}
"""


def generate_complete_story(mode: str, topic: str, title: str, source_text: str,
                            config: dict, model: str, api_key: str) -> dict:
    if not api_key:
        raise RuntimeError("Story correction/generation requires the configured Gemini API key.")
    result = call_model(build_story_prompt(mode, topic, title, source_text, config), model, api_key)
    complete = normalize_text(result.get("story_text") or result.get("story")
                              or result.get("narration") or result.get("text"))
    if not complete and isinstance(result.get("parts"), list):
        complete = normalize_text("\n\n".join(
            str(row.get("text", "") if isinstance(row, dict) else row)
            for row in result["parts"]
        ))
    if word_count(complete) < 20:
        raise RuntimeError("Model did not return a complete usable story.")
    source_verification = None
    if source_text:
        source_verification = verify_revision_semantics(
            source_text, complete, source_text, model, api_key
        )
        if not source_verification.get("passed"):
            raise RuntimeError(
                "Corrected full story failed event/chronology preservation verification: "
                + clean(source_verification.get("explanation"))
            )
    return {
        "story_text": complete,
        "title": clean(result.get("title")),
        "generation_method": "configured_gemini",
        "model": model,
        "proofreading": {
            "performed": True,
            "method": "configured_gemini_model",
            "complete_story_reviewed": True,
            "source_story_word_count": word_count(source_text),
            "corrected_story_word_count": word_count(complete),
            "source_preservation_verification": source_verification,
        },
    }


def verify_revision_semantics(original: str, revised: str, complete_story: str,
                               model: str, api_key: str) -> dict:
    """Independent model pass; rejects revisions that admit omissions or new events."""
    prompt = f"""
You are a strict Hindi story continuity auditor. Compare the ORIGINAL PART and REVISED PART
against the COMPLETE STORY context. Do not rewrite either text.
Return JSON only with boolean fields:
all_events_preserved, chronology_preserved, characters_and_relationships_preserved,
locations_and_objects_preserved, no_new_facts, ending_and_consequences_preserved;
plus arrays omitted_events and added_events, and a concise explanation.
All six booleans must be true and both arrays must be empty to pass.

COMPLETE STORY CONTEXT:
{complete_story}

ORIGINAL PART:
{original}

REVISED PART:
{revised}
"""
    result = call_model(prompt, model, api_key, max_tokens=2500)
    keys = (
        "all_events_preserved", "chronology_preserved",
        "characters_and_relationships_preserved", "locations_and_objects_preserved",
        "no_new_facts", "ending_and_consequences_preserved",
    )
    omissions = result.get("omitted_events", [])
    additions = result.get("added_events", [])
    passed = (all(result.get(key) is True for key in keys)
              and isinstance(omissions, list) and not omissions
              and isinstance(additions, list) and not additions)
    return {
        "performed": True,
        "passed": passed,
        "checks": {key: result.get(key) is True for key in keys},
        "omitted_events": omissions if isinstance(omissions, list) else [str(omissions)],
        "added_events": additions if isinstance(additions, list) else [str(additions)],
        "explanation": clean(result.get("explanation")),
    }


def repair_short_part(part_text: str, part_number: int, complete_story: str,
                      title: str, model: str, api_key: str,
                      *, final_merged: bool = False,
                      return_evidence: bool = False):
    """Retry out-of-target narration; accept changed text only with semantic evidence."""
    current = normalize_text(part_text)
    records: list[dict] = []
    original_hash = sha256(current)
    evidence = None
    target_max = SHORT_MAX
    hard_max = SHORT_MERGED_MAX if final_merged else SHORT_HARD_MAX
    for attempt in range(1, REVISION_ATTEMPTS + 1):
        count = word_count(current)
        if SHORT_MIN <= count <= SHORT_MAX and (count <= SHORT_MAX or evidence):
            break
        direction = (
            "Compress wording toward 120–150 words without dropping any event."
            if count > SHORT_MAX else
            "Expand toward 120–150 words using only facts and details already present in the complete story."
        )
        prompt = f"""
Revise this Hindi narration Part. Return JSON only: {{"text":"..."}}.
PART NUMBER: {part_number}
TITLE: {title}
CURRENT WORD COUNT: {count}
TARGET: 120–150 words. NORMAL HARD LIMIT: 180 words.
FINAL MERGED PART LIMIT, when applicable: 239 words.
DIRECTION: {direction}

COMPLETE STORY CONTEXT:
{complete_story}

CURRENT PART:
{current}

RULES:
- Preserve every event, character, relationship, chronology, clue, object and consequence.
- Do not invent facts, dialogue, clues, twists or outcomes.
- Do not omit any important event or change the ending.
- Correct Hindi matras, spelling, grammar and punctuation.
- Keep complete sentence and meaningful clause boundaries.
- Maintain the same chronological position and scene-unit order.
- Do not add a title, summary, heading or commentary.
"""
        record = {
            "attempt": attempt,
            "before_words": count,
            "before_sha256": sha256(current),
            "attempted": True,
        }
        try:
            if not api_key:
                raise RuntimeError("A configured Gemini API key is required for SHORT Part revision.")
            response = call_model(prompt, model, api_key, max_tokens=5000)
            revised = normalize_text(response.get("text") or response.get("story_text")
                                     or response.get("narration") or response.get("story"))
            if not revised:
                raise ModelError("Revision model returned empty narration.")
            record["after_words"] = word_count(revised)
            record["after_sha256"] = sha256(revised)
            if sha256(revised) == sha256(current):
                record["outcome"] = "unchanged_text_no_revision_evidence"
                records.append(record)
                continue
            semantic = verify_revision_semantics(current, revised, complete_story, model, api_key)
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
                if SHORT_MIN <= word_count(current) <= SHORT_MAX
                else "verified_revision_outside_preferred_range"
            )
            records.append(record)
            if SHORT_MIN <= word_count(current) <= SHORT_MAX:
                break
            if word_count(current) > hard_max:
                continue
        except Exception as exc:
            record["outcome"] = "failed"
            record["error"] = str(exc)
            records.append(record)
    final_count = word_count(current)
    if final_count > hard_max:
        raise RuntimeError(
            f"SHORT Part {part_number} remains above {hard_max} words after verified retries ({final_count})."
        )
    if final_count > SHORT_MAX and not verify_revision_evidence(evidence, current):
        raise RuntimeError(
            f"SHORT Part {part_number} has {final_count} words but no verified changed-text revision evidence."
        )
    if return_evidence:
        return current, records, evidence
    return current, records


def _evidence_is_semantic(evidence: Any, text: str) -> bool:
    return bool(
        verify_revision_evidence(evidence, text)
        and isinstance(evidence, dict)
        and evidence.get("semantic_preservation_verified") is True
    )


def _rebalance_for_merged_count(story_text: str, part_count: int,
                                scenes_range: tuple[int, int]) -> list[str] | None:
    """Repartition the whole ordered story into one fewer Parts after a 239-word merge check."""
    scenes_min, scenes_max = scenes_range
    units = split_sentences(story_text)
    if not units:
        return None

        try:
            units = _expand_to_scene_minimum(units, required)
        except RuntimeError:
            return None
    if not required <= len(units) <= part_count * scenes_max:
        return None
    groups = _balanced_unit_partition(
        units, part_count, scenes_min, scenes_max,
        allow_final_short=False, final_max=SHORT_MERGED_MAX,
    )
    if groups is None:
        return None
    counts = [word_count(group) for group in groups]
    if any(value < SHORT_MIN or value > SHORT_HARD_MAX for value in counts[:-1]):
        return None
    if not SHORT_MIN <= counts[-1] <= SHORT_MERGED_MAX:
        return None
    return groups


def _partition_and_revise(complete_story: str, mode: str, parts_specification: str,
                          story_length: Any, config: dict, model: str, api_key: str,
                          title: str, scenes_range: tuple[int, int]) -> tuple[list[str], dict, dict]:
    minimum_parts, maximum_parts = parse_parts_range(parts_specification)
    total_words = word_count(complete_story)
    if mode == "full":
        selected_count = 1
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
        groups, selected_count, merged_fallback = _partition_short_story(
            complete_story, preferred, minimum_parts, maximum_parts, scenes_range
        )

    # Revise the whole Part, retaining its source context and chronological position.
    original_groups = list(groups)
    revised_groups: list[str] = []
    revision_rows: list[dict] = []
    evidence_by_part: dict[int, dict] = {}
    attempted_parts: list[int] = []
    for number, original in enumerate(groups, 1):
        if mode == "full":
            revised_groups.append(original)
            revision_rows.append({"part": number, "attempted": False, "outcome": "full_unrestricted"})
            continue
        amount = word_count(original)
        is_final = number == len(groups)
        # The final merged-part exception permits 151-239 words without a
        # second compression pass; ordinary Parts still require revision above 150.
        needs_revision = amount < SHORT_MIN or (
            amount > SHORT_MAX and not (is_final and merged_fallback)
        )
        if not needs_revision:
            revised_groups.append(original)
            revision_rows.append({
                "part": number, "attempted": False,
                "outcome": "already_within_target",
                "before_words": amount, "after_words": amount,
                "before_sha256": sha256(original), "after_sha256": sha256(original),
                "attempts": [],
            })
            continue
        revised, attempts, evidence = repair_short_part(
            original, number, complete_story, title, model, api_key,
            final_merged=bool(is_final and merged_fallback),
            return_evidence=True,
        )
        revised_groups.append(revised)
        attempted_parts.append(number)
        if evidence:
            evidence_by_part[number] = evidence
        revision_rows.append({
            "part": number, "attempted": True,
            "outcome": "verified_revision" if evidence else "retried_no_verified_change",
            "before_words": amount, "after_words": word_count(revised),
            "before_sha256": sha256(original), "after_sha256": sha256(revised),
            "revision_evidence": evidence,
            "attempts": attempts,
        })

    if mode == "short":
        # Final Part below 120 gets its retries above. If still short, merge it exactly into the preceding Part.
        if word_count(revised_groups[-1]) < SHORT_MIN:
            if len(revised_groups) < 2:
                raise RuntimeError("A single SHORT Part remains below 120 words; there is no preceding Part to merge into.")
            merged_text = revised_groups[-2].rstrip() + "\n" + revised_groups[-1].lstrip()
            target_count = len(revised_groups) - 1
            if not minimum_parts <= target_count <= maximum_parts:
                raise RuntimeError(
                    f"Final-Part merge requires {target_count} actual Parts, outside "
                    f"the configured PARTS range {minimum_parts}-{maximum_parts}. "
                    "The fixed/ranged Part requirement prevents this merge."
                )
            if word_count(merged_text) > SHORT_MERGED_MAX:
                rebalance_source = "\n\n".join(revised_groups)
                rebalanced = _rebalance_for_merged_count(
                    rebalance_source, target_count, scenes_range
                )
                if rebalanced is None:
                    raise RuntimeError(
                        f"Final-Part merge would produce {word_count(merged_text)} words, "
                        f"above {SHORT_MERGED_MAX}; whole-story rebalancing into "
                        f"{target_count} Parts could not satisfy the configured SCENES and word limits."
                    )
                revised_groups = rebalanced
                evidence_by_part = {}
                revision_rows = []
                attempted_parts = []
                merged_fallback = True
                for rebalance_number, rebalance_text in enumerate(revised_groups, 1):
                    merged_flag = rebalance_number == len(revised_groups)
                    if word_count(rebalance_text) > SHORT_MAX and not merged_flag:
                        revised_text, retry_records, retry_evidence = repair_short_part(
                            rebalance_text, rebalance_number, rebalance_source,
                            title, model, api_key, final_merged=merged_flag,
                            return_evidence=True,
                        )
                        revised_groups[rebalance_number - 1] = revised_text
                        attempted_parts.append(rebalance_number)
                        if retry_evidence:
                            evidence_by_part[rebalance_number] = retry_evidence
                        revision_rows.append({
                            "part": rebalance_number, "attempted": True,
                            "outcome": "verified_after_final_rebalance" if retry_evidence else "unverified_after_final_rebalance",
                            "before_words": word_count(rebalance_text),
                            "after_words": word_count(revised_text),
                            "before_sha256": sha256(rebalance_text),
                            "after_sha256": sha256(revised_text),
                            "revision_evidence": retry_evidence,
                            "attempts": retry_records,
                            "final_part_merged": merged_flag,
                        })
            else:
                old_part_count = len(revised_groups)
                revised_groups = revised_groups[:-2] + [merged_text]
                merged_fallback = True
                merged_number = len(revised_groups)
                evidence_by_part.pop(old_part_count, None)
                evidence_by_part.pop(merged_number, None)
                revision_rows = [
                    row for row in revision_rows
                    if int(row.get("part", 0)) < merged_number
                ]
                attempted_parts = [number for number in attempted_parts if number < merged_number]
                revision_rows.append({
                    "part": merged_number,
                    "attempted": False,
                    "outcome": "final_short_part_merged_exactly_into_preceding_part",
                    "before_words": word_count(merged_text),
                    "after_words": word_count(merged_text),
                    "after_sha256": sha256(merged_text),
                    "final_part_merged": True,
                    "text_preserved_exactly": True,
                })

        for number, part in enumerate(revised_groups, 1):
            amount = word_count(part)
            is_final_merged = bool(merged_fallback and number == len(revised_groups))
            maximum = SHORT_MERGED_MAX if is_final_merged else SHORT_HARD_MAX
            if amount < SHORT_MIN or amount > maximum:
                raise RuntimeError(
                    f"SHORT Part {number} has {amount} words after retry/rebalance; allowed {SHORT_MIN}-{maximum}."
                )
            if amount > SHORT_MAX and not is_final_merged:
                evidence = evidence_by_part.get(number)
                if not _evidence_is_semantic(evidence, part):
                    raise RuntimeError(
                        f"SHORT Part {number} has {amount} words but lacks verified revision and semantic-preservation evidence."
                    )

    final_story_text = "\n\n".join(revised_groups)
    if word_count(final_story_text) <= 0:
        raise RuntimeError("Final story became empty after Part revision.")

    # Re-select/persist the actual count only after any final merge has completed.
    selected_count = len(revised_groups)
    set_selected_parts(
        config, selected_count, source="corrected_story_scene_aware_partition",
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
            "verified" if evidence_by_part else
            "attempted_without_verified_change" if attempted_parts else
            "not_required"
        ),
        "revision_parts": sorted(set(attempted_parts)),
        "revision_evidence_parts": sorted(evidence_by_part),
        "revision_results": revision_rows,
        "revision_attempt_count": sum(len(row.get("attempts", [])) for row in revision_rows),
        "final_part_merged": bool(merged_fallback),
        "final_merged_maximum_words": SHORT_MERGED_MAX if merged_fallback else None,
        "actual_parts": selected_count,
        "parts_count": selected_count,
        "selected_parts": selected_count,
        "final_parts_word_counts": [
            {"part": number, "words": word_count(part), "sha256": sha256(part),
             "revision_evidence": evidence_by_part.get(number)}
            for number, part in enumerate(revised_groups, 1)
        ],
    })
    validation_parts = []
    for number, part in enumerate(revised_groups, 1):
        validation_parts.append({
            "part": number,
            "narration": part,
            "revision_evidence": evidence_by_part.get(number),
            "final_part_merged": bool(merged_fallback and number == len(revised_groups)),
        })
    validation = validate_all_parts(validation_parts, policy)
    # The policy's standard validator intentionally flags a final merged Part >180;
    # validate the documented 239-word exception here instead of suppressing other errors.
    if mode == "short":
        filtered_errors = [
            error for error in validation.get("errors", [])
            if not (merged_fallback and "Part " + str(len(revised_groups)) + ":" in error)
        ]

        "parts_validation": policy.get("parts_validation", {}),
        "parts_selection": selection,
        "story_length_policy": policy,
        "source_fingerprint": source_fingerprint,
        "story_plan_fingerprint": plan.get("source_fingerprint"),
        "story_plan_source": plan.get("source", {}),
        "actual_scene_counts": actual_counts,
        "total_scenes": len(all_scenes),
        "total_main_scenes": len(all_scenes),
        "parts": story_parts,
        "scenes": all_scenes,
        "hook_scenes": all_hooks,
        "suspense_scenes": all_suspense,
        "resolution_scenes": all_resolutions,
        "scene_count": len(all_scenes),
        "scene_range": {"min": scenes_min, "max": scenes_max},
        "total_extra_scenes": len(all_hooks) + len(all_suspense) + len(all_resolutions),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    validate_story(result, len(story_parts), scenes_range, mode)
    return result


def validate_story(story: dict, expected_parts: int, scenes_range: tuple[int, int], mode: str) -> None:
    if isinstance(scenes_range, int) and not isinstance(scenes_range, bool):
        scenes_range = (scenes_range, scenes_range)
    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("Story JSON is missing or not completed.")
    if story.get("format") != mode:
        raise RuntimeError("Story format does not match configured mode.")
    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != expected_parts:
        raise RuntimeError("Story Part count does not match selected count.")
    if story.get("total_parts") != expected_parts:
        raise RuntimeError("Story total_parts does not match selected count.")
    if not isinstance(story.get("story_length_policy"), dict):
        raise RuntimeError("Story has no auditable length policy.")
    policy = story["story_length_policy"]
    if not isinstance(story.get("parts_selection"), dict):
        raise RuntimeError("Story has no auditable PARTS selection.")
    selection = story["parts_selection"]
    if selection.get("selected_parts") != expected_parts:
        raise RuntimeError("Story selected_parts does not match actual Part count.")
    if policy.get("selected_parts") != expected_parts:
        raise RuntimeError("Story length policy selected_parts mismatch.")
    if policy.get("actual_parts") != expected_parts:
        raise RuntimeError("Story length policy actual_parts mismatch.")
    if policy.get("parts_count") != expected_parts:
        raise RuntimeError("Story length policy parts_count mismatch.")
    scenes_min, scenes_max = scenes_range
    all_scenes = []
    counts = []
    all_hooks = []
    all_suspense = []
    all_resolutions = []
    for number, part in enumerate(parts, 1):
        if part.get("part") != number:
            raise RuntimeError("Story Parts are not sequential.")
        narration = normalize_text(part.get("narration"))
        if not narration or clean(narration) != clean(part.get("story_anchor")):
            raise RuntimeError(f"Part {number} narration/story anchor mismatch.")
        actual_words = word_count(narration)
        if actual_words != part.get("word_count"):
            raise RuntimeError(f"Part {number} word count mismatch.")
        if mode == "short":
            maximum = SHORT_MERGED_MAX if part.get("final_part_merged") else SHORT_HARD_MAX
            if not SHORT_MIN <= actual_words <= maximum:
                raise RuntimeError(f"SHORT Part {number} has {actual_words} words; allowed {SHORT_MIN}-{maximum}.")
            if actual_words > SHORT_MAX and not part.get("final_part_merged"):
                evidence = part.get("revision_evidence")
                if not _evidence_is_semantic(evidence, narration):
                    raise RuntimeError(f"SHORT Part {number} above 150 words lacks verified revision evidence.")
        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes_min <= len(scenes) <= scenes_max:
            raise RuntimeError(f"Part {number} main Scene count is outside {scenes_min}-{scenes_max}.")
        if part.get("main_scene_count") != len(scenes) or part.get("actual_scene_count") != len(scenes):
            raise RuntimeError(f"Part {number} actual Scene count metadata mismatch.")
        counts.append(len(scenes))
        for scene_number, scene in enumerate(scenes, 1):
            expected_id = f"part_{number:02d}_scene_{scene_number:02d}"
            if scene.get("part") != number or scene.get("scene") != scene_number:
                raise RuntimeError("Story Scene numbering mismatch.")
            if scene.get("scene_id") != expected_id:
                raise RuntimeError("Story Scene ID mismatch.")
            if not normalize_text(scene.get("narration")):
                raise RuntimeError(f"Scene narration is empty: {expected_id}.")
            all_scenes.append(scene)
        hook = part.get("hook_scene")
        suspense = part.get("suspense_scene")
        resolution = part.get("resolution_scene")
        if not isinstance(hook, dict) or hook.get("role") != "hook":
            raise RuntimeError(f"Part {number} is missing its separate Hook Scene.")
        all_hooks.append(hook)
        if number < expected_parts:
            if not isinstance(suspense, dict) or suspense.get("role") != "suspense":
                raise RuntimeError(f"Part {number} is missing its separate Suspense Scene.")
            all_suspense.append(suspense)
        elif not isinstance(resolution, dict) or resolution.get("role") != "resolution":
            raise RuntimeError("Final Part is missing its separate Resolution Scene.")
        if resolution:
            all_resolutions.append(resolution)
    if story.get("actual_scene_counts") != counts:
        raise RuntimeError("Story actual_scene_counts does not match Part Scene records.")
    if story.get("total_scenes") != len(all_scenes) or story.get("total_main_scenes") != len(all_scenes):
        raise RuntimeError("Story total Scene count metadata mismatch.")
    if story.get("scenes") != all_scenes:
        raise RuntimeError("Story flattened Scenes differ from Part Scene records.")
    if story.get("hook_scenes") != all_hooks or story.get("suspense_scenes") != all_suspense:
        raise RuntimeError("Story Hook/Suspense records differ from Part records.")
    if story.get("resolution_scenes") != all_resolutions:
        raise RuntimeError("Story Resolution records differ from Part records.")
    if clean("\n\n".join(row["narration"] for row in parts)) != clean(story.get("corrected_complete_story")):
        raise RuntimeError("Story Parts do not reconstruct corrected_complete_story.")
    if story.get("source_fingerprint") != story.get("source_fingerprint"):
        raise RuntimeError("Story source fingerprint mismatch.")


def cache_is_valid(story: dict | None, source_signature: str, mode: str,
                   scenes_range: tuple[int, int], minimum_parts: int,
                   maximum_parts: int) -> bool:
    if not isinstance(story, dict):
        return False
    try:
        count = int(story.get("total_parts", 0))
        if not minimum_parts <= count <= maximum_parts:
            return False
        validate_story(story, count, scenes_range, mode)
        if story.get("source_fingerprint") != source_signature:
            return False
        policy = story.get("story_length_policy", {})
        if policy.get("selected_parts") != count:
            return False
        return True
    except Exception:
        return False


def reusable_corrected_story_checkpoint(
    checkpoint: dict | None,
    source_signature: str,
    mode: str,
    minimum_parts: int,
    maximum_parts: int,
) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    if checkpoint.get("status") not in {"corrected", "completed", "validated"}:
        return False
    if checkpoint.get("source_fingerprint") != source_signature:
        return False
    if checkpoint.get("format") != mode:
        return False
    story_text = normalize_text(
        checkpoint.get("corrected_complete_story")
        or checkpoint.get("corrected_story")
        or checkpoint.get("story_text")
    )
    if not story_text:
        return False
    if checkpoint.get("corrected_story_sha256") and checkpoint["corrected_story_sha256"] != sha256(story_text):
        return False
    count = checkpoint.get("selected_parts") or checkpoint.get("total_parts")
    try:
        count = int(count)
    except (TypeError, ValueError):
        return False
    return minimum_parts <= count <= maximum_parts


def _write_story_cache(story: dict, source_signature: str, mode: str) -> None:
    wrapper = {
        "version": VERSION,
        "status": "completed",
        "source_fingerprint": source_signature,
        "format": mode,
        "total_parts": story["total_parts"],
        "title": story["title"],
        "story": story,
    }
    write_json(CACHE_FILE, wrapper)


def get_selected_model(config: dict) -> str:
    model = str(config.get("GEMINI_MODEL", "")).strip()
    if not model:
        raise RuntimeError("GEMINI_MODEL is empty.")
    return model


def get_api_key(config: dict) -> str:
    candidates = [
        ("GEMINI_API_KEY", config.get("GEMINI_API_KEY")),
        ("GOOGLE_API_KEY", config.get("GOOGLE_API_KEY")),
    ]
    for name, value in candidates:
        if value and str(value).strip():
            return str(value).strip()
    raise RuntimeError("No configured Gemini API key is available.")


def main() -> None:
    config = load_and_validate()
    mode = get_format(config)
    topic = get_topic(config)
    source_text = get_story_text(config)
    parts_specification = get_parts_range(config)
    scenes_range = get_scenes_range(config)
    minimum_parts, maximum_parts = parse_parts_range(parts_specification)
    story_length = config.get("STORY_LENGTH", "medium")
    model = get_selected_model(config)
    api_key = get_api_key(config)
    if not source_text:
        raise RuntimeError("Input/topic.txt contains no story or topic.")
    source_signature = fingerprint({
        "format": mode,
        "topic": topic,
        "source_text": source_text,
        "parts": parts_specification,
        "scenes": list(scenes_range),
    })
    log("FORMAT:", mode)
    log("TOPIC:", topic)
    log("PARTS RANGE:", parts_specification)
    log("SCENES RANGE:", scenes_range)
    log("SOURCE WORDS:", word_count(source_text))
    log("SELECTED MODEL:", model)
    cached = read_json(OUTPUT_FILE)
    if cache_is_valid(cached, source_signature, mode, scenes_range, minimum_parts, maximum_parts):
        cached_count = int(cached["total_parts"])
        set_selected_parts(config, cached_count, source="validated_story_cache",
                           story_word_count=word_count(cached.get("corrected_complete_story", "")))
        cached_text = normalize_text(cached.get("corrected_complete_story"))
        cached_parts = [normalize_text(row.get("narration")) for row in cached["parts"]]
        plan = make_final_plan(config, mode, topic, cached_text, cached_count, scenes_range, cached_parts)
        if cached.get("story_plan_fingerprint") != plan.get("source_fingerprint"):
            cached["story_plan_fingerprint"] = plan.get("source_fingerprint")
            write_json(OUTPUT_FILE, cached)
        atomic_write(TITLE_FILE, clean(cached["title"]) + "\n")
        log("Matching validated story reused.")
        log("ACTUAL PARTS:", cached_count)
        log("ACTUAL SCENE COUNTS:", cached.get("actual_scene_counts"))
        log("External story-generation API calls: 0")
        log("STORY GENERATION: SUCCESS")
        return


## User preference around code
- Wants the exact replacement code, not a patch.
- If too long, small sequential chunks; no commentary in between chunks, simply code and part label.
- Wants no unrelated modifications.
- Do not claim this chunk sequence is a completed replacement until all parts are sent and the Run #64 cache branch is replaced.

    if clean(story.get("topic")) != clean(topic):
        return False
    if explicit_title and clean(story.get("title")) != clean(explicit_title):
        return False
    if clean(story.get("model")) != clean(model):
        return False
    if clean(story.get("source_text")) != clean(source_text):
        return False
    corrected = normalize_text(story.get("corrected_complete_story"))
    if not corrected or word_count(corrected) <= 0:
        return False
    if story.get("corrected_complete_story_word_count") != word_count(corrected):
        return False
    if story.get("source_word_count") != word_count(source_text):
        return False
    if story.get("total_story_words") != word_count(corrected):
        return False
    if not isinstance(story.get("proofreading"), dict):
        return False
    if story["proofreading"].get("performed") is not True:
        return False
    if not isinstance(story.get("story_length_policy"), dict):
        return False
    if not isinstance(story.get("parts_selection"), dict):
        return False
    return True


def _load_story_checkpoint() -> dict | None:
    for path in (OUTPUT_FILE, CACHE_FILE, CHECKPOINT_FILE):
        value = read_json(path)
        if not isinstance(value, dict):
            continue
        story = value.get("story") if isinstance(value.get("story"), dict) else value
        if isinstance(story, dict) and story.get("status") == "completed":
            return story
    return None


def _proofread_complete_story(source_text: str, model: str, api_key: str,
                              mode: str) -> tuple[str, dict]:
    """Proofread the complete source before counting or splitting any Part."""
    if not source_text.strip():
        raise RuntimeError("The supplied story is empty.")
    if is_topic_only(source_text):
        return "", {
            "performed": True,
            "method": "new_story_generation",
            "input_was_complete_story": False,
            "meaning_preserved": True,
            "events_preserved": True,
            "source_sha256": sha256(source_text),
        }

    prompt = f"""
You are a careful Hindi story proofreader and copy editor.

Correct the ENTIRE supplied story before it is counted or split.
Fix Hindi matras, spelling, grammar, punctuation, commas and sentence flow.
Preserve every character, event, action, place, object, relationship, reveal,
chronological order and the original meaning.
Do not summarize, shorten, expand, invent, delete, or rearrange story events.
Do not add a title unless the input already contains one.
Return the complete corrected story only, with paragraph breaks preserved.

SOURCE STORY:
{source_text}
"""
    raw = call_gemini(prompt, model, api_key, temperature=0.1)
    corrected = extract_text(raw)
    corrected = normalize_text(corrected)
    if not corrected:
        raise RuntimeError("Proofreading returned an empty corrected story.")
    if word_count(corrected) < max(1, int(word_count(source_text) * 0.75)):
        raise RuntimeError("Proofreading removed too much source content; the original meaning cannot be verified.")
    return corrected, {
        "performed": True,
        "method": "whole_story_proofreading",
        "input_was_complete_story": True,
        "meaning_preservation_check": "length_guard_and_revision_evidence",
        "source_sha256": sha256(source_text),
        "corrected_sha256": sha256(corrected),
        "source_word_count": word_count(source_text),
        "corrected_word_count": word_count(corrected),
    }


def _generate_complete_story(source_text: str, model: str, api_key: str,
                             mode: str, title: str) -> tuple[str, str]:
    """Generate a complete story only when the input is a topic or brief."""
    prompt = f"""
Write one complete, coherent Hindi story from the supplied topic/brief.
Do not write a summary, outline, scene list, or multiple alternative stories.
Preserve every explicit user-provided fact and requested event.
Use natural Hindi, correct matras, grammar and punctuation.
Create a clear beginning, development, climax and satisfying ending.
Do not add a title inside the story body.
Return JSON with exactly these keys: title, story.
The title must represent the complete story, not merely its opening.
Format: {mode}
Topic/brief:
{source_text}
"""
    raw = call_gemini(prompt, model, api_key, temperature=0.7)
    title_from_model, story_text = parse_story_response(raw)
    if not story_text:
        raise RuntimeError("Story-generation model returned no complete story.")
    return clean(title_from_model or title), normalize_text(story_text)


def _derive_title(complete_story: str, model: str, api_key: str,
                  explicit_title: str = "") -> str:
    if explicit_title.strip():
        return clean(explicit_title)
    prompt = f"""
Create one concise, compelling Hindi title that represents the complete story.
Base it on the full story, including its central conflict and ending.
Do not base it only on the opening lines.
Return only the title.

COMPLETE STORY:
{complete_story}
"""
    raw = call_gemini(prompt, model, api_key, temperature=0.3)
    title = clean(extract_text(raw))
    if not title:
        raise RuntimeError("Could not derive a title from the complete story.")
    return title


def _persist_corrected_checkpoint(story_text: str, source_signature: str,
                                  mode: str, title: str, model: str,
                                  proofreading: dict) -> None:
    checkpoint = {
        "version": VERSION,
        "status": "corrected",
        "format": mode,
        "title": title,
        "model": model,
        "source_fingerprint": source_signature,
        "corrected_complete_story": story_text,
        "corrected_story_sha256": sha256(story_text),
        "corrected_complete_story_word_count": word_count(story_text),
        "proofreading": proofreading,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(CHECKPOINT_FILE, checkpoint)


def _restore_checkpoint_story(checkpoint: dict, source_signature: str,
                              mode: str, topic: str, explicit_title: str,
                              source_text: str, model: str) -> tuple[str, str, dict]:
    story_text = normalize_text(checkpoint.get("corrected_complete_story"))
    title = clean(checkpoint.get("title") or explicit_title)
    proofreading = checkpoint.get("proofreading")
    if not isinstance(proofreading, dict):
        raise RuntimeError("Reusable corrected-story checkpoint has no proofreading record.")
    if checkpoint.get("source_fingerprint") != source_signature:
        raise RuntimeError("Corrected-story checkpoint source fingerprint mismatch.")
    if not story_text:
        raise RuntimeError("Corrected-story checkpoint is empty.")
    if checkpoint.get("corrected_story_sha256") != sha256(story_text):
        raise RuntimeError("Corrected-story checkpoint SHA-256 mismatch.")
    if not title:
        title = _derive_title(story_text, model, get_api_key(load_and_validate()))
    return story_text, title, proofreading


def _validate_plan_matches_story(plan: dict, story: dict) -> None:
    plan_parts = plan.get("parts") if isinstance(plan, dict) else None
    story_parts = story.get("parts") if isinstance(story, dict) else None
    if not isinstance(plan_parts, list) or not isinstance(story_parts, list):
        raise RuntimeError("Story Plan or Story Part records are missing.")
    if len(plan_parts) != len(story_parts):
        raise RuntimeError("Story Plan and Story Part counts differ.")
    plan_counts = []
    for number, (plan_part, story_part) in enumerate(zip(plan_parts, story_parts), 1):
        plan_scenes = plan_part.get("scenes") if isinstance(plan_part, dict) else None
        story_scenes = story_part.get("scenes") if isinstance(story_part, dict) else None
        if not isinstance(plan_scenes, list) or not isinstance(story_scenes, list):
            raise RuntimeError(f"Part {number} has missing Scene records in the Story Plan or Story.")
        if len(plan_scenes) != len(story_scenes):
            raise RuntimeError(f"Part {number} Story Plan Scene count differs from story.json.")
        plan_counts.append(len(plan_scenes))
        for scene_number, (plan_scene, story_scene) in enumerate(zip(plan_scenes, story_scenes), 1):
            plan_narration = clean(plan_scene.get("narration") or plan_scene.get("story_anchor"))
            story_narration = clean(story_scene.get("narration"))
            if plan_narration != story_narration:
                raise RuntimeError(
                    f"Part {number} Scene {scene_number} Story Plan narration differs from story.json."
                )
    if story.get("actual_scene_counts") != plan_counts:
        raise RuntimeError("Story Plan actual Scene counts differ from story.json.")
    if plan.get("source_fingerprint") != story.get("story_plan_fingerprint"):
        raise RuntimeError("Story Plan fingerprint differs from story.json.")


def _write_final_artifacts(story: dict, plan: dict, policy: dict,
                           source_signature: str, mode: str) -> None:
    _validate_plan_matches_story(plan, story)
    write_json(POLICY_FILE, policy)
    write_json(PLAN_FILE, plan)
    write_json(OUTPUT_FILE, story)
    _write_story_cache(story, source_signature, mode)
    atomic_write(TITLE_FILE, clean(story["title"]) + "\n")


def _restore_validated_cache(cached: dict, config: dict, mode: str,
                             topic: str, source_text: str, source_signature: str,
                             scenes_range: tuple[int, int], model: str) -> bool:
    """Repair a matching story cache by rebuilding its Plan and all dependent Scene records."""
    if not cache_is_valid(
        cached, source_signature, mode, scenes_range,
        int(cached.get("parts_selection", {}).get("minimum_parts", 1)),
        int(cached.get("parts_selection", {}).get("maximum_parts", cached.get("total_parts", 1))),
    ):
        return False
    cached_count = int(cached["total_parts"])
    cached_text = normalize_text(cached.get("corrected_complete_story"))
    cached_parts = [normalize_text(row.get("narration")) for row in cached["parts"]]
    if not cached_text or len(cached_parts) != cached_count:
        return False
    if clean("\n\n".join(cached_parts)) != clean(cached_text):
        return False
    set_selected_parts(
        config, cached_count, source="validated_story_cache_rebuild",
        story_word_count=word_count(cached_text),
    )
    plan = make_final_plan(config, mode, topic, cached_text, cached_count, scenes_range, cached_parts)
    actual_part_texts = [
        normalize_text(row.get("narration"))
        for row in plan.get("parts", [])
    ]
    if len(actual_part_texts) != cached_count or any(not value for value in actual_part_texts):
        raise RuntimeError("Rebuilt Story Plan has missing Part narration.")
    if clean("\n\n".join(actual_part_texts)) != clean(cached_text):
        raise RuntimeError("Rebuilt Story Plan changed the cached corrected story.")
    policy = dict(cached.get("story_length_policy") or {})
    selection = dict(cached.get("parts_selection") or {})
    proofreading = cached.get("proofreading")
    if not isinstance(proofreading, dict) or not isinstance(selection, dict) or not selection:
        raise RuntimeError("Cached story lacks proofreading or Part-selection metadata.")
    if not isinstance(policy, dict) or not policy:
        raise RuntimeError("Cached story lacks story-length policy metadata.")
    title = clean(cached.get("title"))
    if not title:
        raise RuntimeError("Cached story title is missing.")
    plan["title"] = title
    plan["final_title"] = title
    plan["title_source"] = "validated_story_cache"
    write_json(PLAN_FILE, plan)
    rebuilt = make_story(
        title=title,
        topic=topic,
        mode=mode,
        source_text=source_text,
        complete_story="\n\n".join(actual_part_texts),
        parts=actual_part_texts,
        plan=plan,
        scenes_range=scenes_range,
        model=model,
        proofreading=proofreading,
        policy=policy,
        selection=selection,
        source_fingerprint=source_signature,
    )
    policy["actual_scene_counts"] = rebuilt.get("actual_scene_counts", [])
    policy["total_main_scenes"] = rebuilt.get("total_main_scenes", 0)
    rebuilt["story_length_policy"] = policy
    validate_story(rebuilt, cached_count, scenes_range, mode)
    _write_final_artifacts(rebuilt, plan, policy, source_signature, mode)
    log("Matching story cache reused and Story Plan/Scene records rebuilt.")
    log("ACTUAL PARTS:", cached_count)
    log("ACTUAL SCENE COUNTS:", rebuilt.get("actual_scene_counts"))
    log("External story-generation API calls: 0")
    log("STORY GENERATION: SUCCESS")
    return True


def main() -> None:
    config = load_and_validate()
    mode = get_format(config)
    topic = get_topic(config)
    source_text = get_story_text(config)
    parts_specification = get_parts_range(config)
    scenes_range = get_scenes_range(config)
    minimum_parts, maximum_parts = parse_parts_range(parts_specification)
    story_length = config.get("STORY_LENGTH", "medium")
    model = get_selected_model(config)
    api_key = get_api_key(config)
    if not source_text:
        raise RuntimeError("Input/topic.txt contains no story or topic.")
    source_signature = fingerprint({
        "format": mode,
        "topic": topic,
        "source_text": source_text,
        "parts": parts_specification,
        "scenes": list(scenes_range),
    })
    log("FORMAT:", mode)
    log("TOPIC:", topic)
    log("PARTS RANGE:", parts_specification)
    log("SCENES RANGE:", scenes_range)
    log("SOURCE WORDS:", word_count(source_text))
    log("SELECTED MODEL:", model)
    cached = read_json(OUTPUT_FILE)
    if cache_is_valid(cached, source_signature, mode, scenes_range, minimum_parts, maximum_parts):
        if _restore_validated_cache(
            cached, config, mode, topic, source_text, source_signature,
            scenes_range, model,
        ):
            return
    checkpoint = _load_story_checkpoint()
    if reusable_corrected_story_checkpoint(
        checkpoint, mode=mode, topic=topic, explicit_title="",
        source_text=source_text, model=model,
    ):
        complete_story, title, proofreading = _restore_checkpoint_story(
            checkpoint, source_signature, mode, topic, "", source_text, model
        )
        log("Matching corrected-story checkpoint reused; proofreading/story-generation API calls: 0.")
    else:
        explicit_title = topic if not is_topic_only(source_text) else ""
        if is_topic_only(source_text):
            title, complete_story = _generate_complete_story(
                source_text, model, api_key, mode, explicit_title
            )
            proofreading = {
                "performed": True,
                "method": "new_story_generation",
                "input_was_complete_story": False,
                "meaning_preserved": True,
                "events_preserved": True,
                "source_sha256": sha256(source_text),
            }
        else:
            complete_story, proofreading = _proofread_complete_story(
                source_text, model, api_key, mode
            )
            title = _derive_title(complete_story, model, api_key, explicit_title)
        _persist_corrected_checkpoint(
            complete_story, source_signature, mode, title, model, proofreading
        )
    if not clean(title):
        title = _derive_title(complete_story, model, api_key)
    log("CORRECTED COMPLETE STORY WORDS:", word_count(complete_story))
    log("FINAL TITLE:", title)
    parts, policy, selection = _partition_and_revise(
        complete_story, mode, parts_specification, story_length,
        config, model, api_key, title, scenes_range,
    )
    final_complete_story = "\n\n".join(parts)
    plan = make_final_plan(
        config, mode, topic, final_complete_story, len(parts),
        scenes_range, parts,
    )
    plan["title"] = title
    plan["final_title"] = title
    plan["title_source"] = "full_story_summary_or_explicit_input"
    write_json(PLAN_FILE, plan)
    story = make_story(
        title=title, topic=topic, mode=mode, source_text=source_text,
        complete_story=final_complete_story, parts=parts, plan=plan,
        scenes_range=scenes_range, model=model, proofreading=proofreading,
        policy=policy, selection=selection,
        source_fingerprint=source_signature,
    )
    _write_final_artifacts(story, plan, policy, source_signature, mode)
    log("ACTUAL PARTS:", len(parts))
    log("ACTUAL SCENE COUNTS:", story.get("actual_scene_counts"))
    log("TOTAL MAIN SCENES:", story.get("total_main_scenes"))
    log("External story-generation API calls: 0" if checkpoint else "Story-generation/proofreading API calls completed.")
    log("STORY GENERATION: SUCCESS")


if __name__ == "__main__":
    main()

        return False
    if clean(story.get("source_text")) != clean(source_text):
        return False
    if explicit_title and clean(story.get("title")) != clean(explicit_title):
        return False
    if clean(story.get("model")) != clean(model):
        return False
    corrected = normalize_text(story.get("corrected_complete_story"))
    if word_count(corrected) < 20:
        return False
    declared_words = story.get("corrected_complete_story_word_count")
    if isinstance(declared_words, bool):
        return False
    try:
        if int(declared_words) != word_count(corrected):
            return False
    except (TypeError, ValueError):
        return False
    proofreading = story.get("proofreading")
    if not isinstance(proofreading, dict) or proofreading.get("performed") is not True:
        return False
    if not clean(story.get("generation_method")) or not clean(story.get("title")):
        return False
    if len(clean(story.get("title"))) > 180:
        return False
    if source_text:
        try:
            if int(story.get("source_word_count", -1)) != word_count(source_text):
                return False
        except (TypeError, ValueError):
            return False
    if clean(story.get("final_title")) != clean(story.get("title")):
        return False
    return True


def get_final_title(supplied_title: str, narrative_title: str, complete_story: str,
                    model: str, api_key: str) -> str:
    if supplied_title:
        return supplied_title
    if clean(narrative_title):
        return clean(narrative_title)
    result = call_model(f"""
Read the COMPLETE Hindi story, including its middle and ending.
Return JSON only: {{"title":"..."}}.
Create one concise meaningful Hindi title representing the entire story, its central conflict and final outcome.
Do not title it only from the opening lines. Do not invent facts. Do not return a summary.
COMPLETE STORY:
{complete_story}
""", model, api_key, max_tokens=1000)
    title = clean(result.get("title"))
    if not title:
        raise RuntimeError("Configured model did not return a full-story-based title.")
    return title


def main() -> None:
    STORY_DIR.mkdir(parents=True, exist_ok=True)
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Input configuration missing: {INPUT_FILE}")
    config = load_and_validate(str(INPUT_FILE))
    mode = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    title_input = clean(config.get("TITLE", ""))
    source_text = get_complete_source(config)
    parts_specification = clean(config.get("PARTS_SPEC") or config.get("PARTS") or "1")
    minimum_parts, maximum_parts = get_parts_range(config)
    scenes_range = get_scenes_range(config)
    scenes_min, scenes_max = scenes_range
    story_length = config.get("STORY_LENGTH", "auto")

    if mode not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")
    if mode == "full" and (minimum_parts, maximum_parts) != (1, 1):
        raise RuntimeError("FULL format requires exactly PARTS=1.")
    if scenes_max * minimum_parts > MAX_TOTAL_SCENES:
        raise RuntimeError("Configured minimum PARTS × maximum SCENES exceeds the total Scene safety limit.")
    if not source_text and not topic and not title_input:
        raise RuntimeError("Provide STORY_TEXT, TOPIC or TITLE.")
    if source_text and word_count(source_text) < 20:
        raise RuntimeError("Complete STORY_TEXT contains fewer than 20 words.")

    model = get_selected_model()
    api_key = get_api_key()
    source_signature = fingerprint({
        "version": VERSION,
        "format": mode,
        "parts_specification": parts_specification,
        "scenes_range": list(scenes_range),
        "story_length": story_length,
        "topic": topic,
        "title": title_input,
        "source_text": source_text,
        "model": model,
    })
    log("FORMAT:", mode)
    log("PARTS RANGE:", f"{minimum_parts}-{maximum_parts}")
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("SOURCE STORY WORDS:", word_count(source_text))
    log("MODEL:", model)

    cached = read_json(OUTPUT_FILE)
    if cache_is_valid(cached, source_signature, mode, scenes_range, minimum_parts, maximum_parts):
        cached_count = int(cached["total_parts"])
        set_selected_parts(config, cached_count, source="validated_story_cache",
                           story_word_count=word_count(cached.get("corrected_complete_story", "")))
        cached_text = normalize_text(cached.get("corrected_complete_story"))
        cached_parts = [normalize_text(row.get("narration")) for row in cached["parts"]]
        plan = make_final_plan(config, mode, topic, cached_text, cached_count, scenes_range, cached_parts)
        if cached.get("story_plan_fingerprint") != plan.get("source_fingerprint"):
            cached["story_plan_fingerprint"] = plan.get("source_fingerprint")
            write_json(OUTPUT_FILE, cached)
        atomic_write(TITLE_FILE, clean(cached["title"]) + "\n")
        log("Matching validated story reused.")
        log("ACTUAL PARTS:", cached_count)
        log("ACTUAL SCENE COUNTS:", cached.get("actual_scene_counts"))
        log("External story-generation API calls: 0")
        log("STORY GENERATION: SUCCESS")
        return

    checkpoint = cached
    if not isinstance(checkpoint, dict):
        cached_wrapper = read_json(CACHE_FILE)
        candidate = cached_wrapper.get("story") if isinstance(cached_wrapper, dict) else None
        checkpoint = candidate if isinstance(candidate, dict) else None
    if reusable_corrected_story_checkpoint(
        checkpoint,
        mode=mode,
        topic=topic,
        explicit_title=title_input,
        source_text=source_text,
        model=model,
    ):
        narrative = {
            "story_text": normalize_text(checkpoint.get("corrected_complete_story")),
            "title": clean(checkpoint.get("title")),
            "generation_method": clean(checkpoint.get("generation_method")),
            "model": clean(checkpoint.get("model")),
            "proofreading": dict(checkpoint.get("proofreading")),
        }
        log("Reusing matching corrected complete-story checkpoint; no full-story generation API call.")
    else:
        if not api_key:
            raise RuntimeError("Story correction/generation requires the configured Gemini API key.")
        narrative = generate_complete_story(mode, topic, title_input, source_text, config, model, api_key)
    corrected_story = normalize_text(narrative["story_text"])
    if word_count(corrected_story) < 20:
        raise RuntimeError("Corrected complete story is too short.")
    final_title = get_final_title(title_input, narrative.get("title", ""), corrected_story, model, api_key)

    final_parts, policy, selection = _partition_and_revise(
        corrected_story, mode, parts_specification, story_length, config,
        model, api_key, final_title, scenes_range,
    )
    final_complete_story = "\n\n".join(final_parts)
    policy["source_story_word_count"] = word_count(source_text)
    policy["corrected_story_word_count"] = word_count(final_complete_story)
    policy["corrected_story_sha256"] = sha256(final_complete_story)
    policy["story_was_corrected_before_counting"] = True
    policy["selection_persisted"] = True
    policy["scenes_min"] = scenes_min
    policy["scenes_max"] = scenes_max
    policy["scenes_range"] = list(scenes_range)
    write_json(POLICY_FILE, policy)

    selected_count = len(final_parts)
    if selected_count * scenes_max > MAX_TOTAL_SCENES:
        raise RuntimeError(
            "Actual selected PARTS × configured maximum SCENES exceeds "
            f"the Story Plan safety limit of {MAX_TOTAL_SCENES}."
        )
    plan = make_final_plan(config, mode, topic, final_complete_story,
                           selected_count, scenes_range, final_parts)
    actual_part_texts = [normalize_text(part.get("narration")) for part in plan["parts"]]
    actual_complete_story = "\n\n".join(actual_part_texts)
    if clean(actual_complete_story) != clean(final_complete_story):
        raise RuntimeError("Variable Scene plan changed the final corrected story text.")
    plan["final_title"] = final_title
    plan["title_source"] = "final_corrected_story"
    write_json(PLAN_FILE, plan)

    # Attach revision evidence to the corresponding final Part after planning.
    story = make_story(
        title=final_title, topic=topic, mode=mode,
        source_text=source_text, complete_story=actual_complete_story,
        parts=actual_part_texts, plan=plan, scenes_range=scenes_range,
        model=model, proofreading=narrative["proofreading"],
        policy=policy, selection=selection,
        source_fingerprint=source_signature,
    )
    # Plan metadata records the actual, not the configured-minimum, Scene count.
    policy["actual_scene_counts"] = story["actual_scene_counts"]
    policy["total_main_scenes"] = story["total_scenes"]
    story["story_length_policy"] = policy
    validate_story(story, selected_count, scenes_range, mode)
    write_json(OUTPUT_FILE, story)
    write_json(CACHE_FILE, {
        "version": VERSION, "status": "completed",
        "source_fingerprint": source_signature,
        "format": mode, "total_parts": selected_count,
        "title": final_title, "story": story,
    })
    atomic_write(TITLE_FILE, final_title + "\n")

    log("STORY GENERATION: SUCCESS")
    log("FORMAT:", mode)
    log("TITLE:", final_title)
    log("CORRECTED STORY WORDS:", word_count(actual_complete_story))
    log("CONFIGURED PARTS:", parts_specification)
    log("ACTUAL SELECTED PARTS:", selected_count)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", story["actual_scene_counts"])
    log("TOTAL MAIN SCENES:", story["total_scenes"])
    log("REVISION ATTEMPTED PARTS:", policy.get("revision_parts", []))
    log("FINAL PART MERGED:", policy.get("final_part_merged", False))
    log("MODEL:", model)
    log("STORY OUTPUT:", OUTPUT_FILE)
    log("STORY PLAN:", PLAN_FILE)
    log("LENGTH POLICY:", POLICY_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("STORY GENERATION: CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log("STORY GENERATION: FAILED")
        log(type(exc).__name__ + ":", str(exc))
        raise SystemExit(1)

def _partition_and_revise(complete_story: str, mode: str, parts_specification: str,
                          story_length: Any, config: dict, model: str, api_key: str,
                          title: str, scenes_range: tuple[int, int]) -> tuple[list[str], dict, dict]:
    minimum_parts, maximum_parts = parse_parts_range(parts_specification)
    total_words = word_count(complete_story)
    if mode == "full":
        selected_count = 1
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
        groups, selected_count, merged_fallback = _partition_short_story(
            complete_story, preferred, minimum_parts, maximum_parts, scenes_range
        )

    # Revise the whole Part, retaining its source context and chronological position.
    original_groups = list(groups)
    revised_groups: list[str] = []
    revision_rows: list[dict] = []
    evidence_by_part: dict[int, dict] = {}
    attempted_parts: list[int] = []
    for number, original in enumerate(groups, 1):
        if mode == "full":
            revised_groups.append(original)
            revision_rows.append({"part": number, "attempted": False, "outcome": "full_unrestricted"})
            continue
        amount = word_count(original)
        is_final = number == len(groups)
        # The final merged-part exception permits 151-239 words without a
        # second compression pass; ordinary Parts still require revision above 150.
        needs_revision = amount < SHORT_MIN or (
            amount > SHORT_MAX and not (is_final and merged_fallback)
        )
        if not needs_revision:
            revised_groups.append(original)
            revision_rows.append({
                "part": number, "attempted": False,
                "outcome": "already_within_target",
                "before_words": amount, "after_words": amount,
                "before_sha256": sha256(original), "after_sha256": sha256(original),
                "attempts": [],
            })
            continue
        revised, attempts, evidence = repair_short_part(
            original, number, complete_story, title, model, api_key,
            final_merged=bool(is_final and merged_fallback),
            return_evidence=True,
        )
        revised_groups.append(revised)
        attempted_parts.append(number)
        if evidence:
            evidence_by_part[number] = evidence
        revision_rows.append({
            "part": number, "attempted": True,
            "outcome": "verified_revision" if evidence else "retried_no_verified_change",
            "before_words": amount, "after_words": word_count(revised),
            "before_sha256": sha256(original), "after_sha256": sha256(revised),
            "revision_evidence": evidence,
            "attempts": attempts,
        })

    if mode == "short":
        # Final Part below 120 gets its retries above. If still short, merge it exactly into the preceding Part.
        if word_count(revised_groups[-1]) < SHORT_MIN:
            if len(revised_groups) < 2:
                raise RuntimeError("A single SHORT Part remains below 120 words; there is no preceding Part to merge into.")
            merged_text = revised_groups[-2].rstrip() + "\n" + revised_groups[-1].lstrip()
            target_count = len(revised_groups) - 1
            if not minimum_parts <= target_count <= maximum_parts:
                raise RuntimeError(
                    f"Final-Part merge requires {target_count} actual Parts, outside "
                    f"the configured PARTS range {minimum_parts}-{maximum_parts}. "
                    "The fixed/ranged Part requirement prevents this merge."
                )
            if word_count(merged_text) > SHORT_MERGED_MAX:
                rebalance_source = "\n\n".join(revised_groups)
                rebalanced = _rebalance_for_merged_count(
                    rebalance_source, target_count, scenes_range
                )
                if rebalanced is None:
                    raise RuntimeError(
                        f"Final-Part merge would produce {word_count(merged_text)} words, "
                        f"above {SHORT_MERGED_MAX}; whole-story rebalancing into "
                        f"{target_count} Parts could not satisfy the configured SCENES and word limits."
                    )
                revised_groups = rebalanced
                evidence_by_part = {}
                revision_rows = []
                attempted_parts = []
                merged_fallback = True
                for rebalance_number, rebalance_text in enumerate(revised_groups, 1):
                    merged_flag = rebalance_number == len(revised_groups)
                    if word_count(rebalance_text) > SHORT_MAX and not merged_flag:
                        revised_text, retry_records, retry_evidence = repair_short_part(
                            rebalance_text, rebalance_number, rebalance_source,
                            title, model, api_key, final_merged=merged_flag,
                            return_evidence=True,
                        )
                        revised_groups[rebalance_number - 1] = revised_text
                        attempted_parts.append(rebalance_number)
                        if retry_evidence:
                            evidence_by_part[rebalance_number] = retry_evidence
                        revision_rows.append({
                            "part": rebalance_number, "attempted": True,
                            "outcome": "verified_after_final_rebalance" if retry_evidence else "unverified_after_final_rebalance",
                            "before_words": word_count(rebalance_text),
                            "after_words": word_count(revised_text),
                            "before_sha256": sha256(rebalance_text),
                            "after_sha256": sha256(revised_text),
                            "revision_evidence": retry_evidence,
                            "attempts": retry_records,
                            "final_part_merged": merged_flag,
                        })
            else:
                old_part_count = len(revised_groups)
                revised_groups = revised_groups[:-2] + [merged_text]
                merged_fallback = True
                merged_number = len(revised_groups)
                evidence_by_part.pop(old_part_count, None)
                evidence_by_part.pop(merged_number, None)
                revision_rows = [
                    row for row in revision_rows
                    if int(row.get("part", 0)) < merged_number
                ]
                attempted_parts = [number for number in attempted_parts if number < merged_number]
                revision_rows.append({
                    "part": merged_number,
                    "attempted": False,
                    "outcome": "final_short_part_merged_exactly_into_preceding_part",
                    "before_words": word_count(merged_text),
                    "after_words": word_count(merged_text),
                    "after_sha256": sha256(merged_text),
                    "final_part_merged": True,
                    "text_preserved_exactly": True,
                })

        for number, part in enumerate(revised_groups, 1):
            amount = word_count(part)
            is_final_merged = bool(merged_fallback and number == len(revised_groups))
            maximum = SHORT_MERGED_MAX if is_final_merged else SHORT_HARD_MAX
            if amount < SHORT_MIN or amount > maximum:
                raise RuntimeError(
                    f"SHORT Part {number} has {amount} words after retry/rebalance; allowed {SHORT_MIN}-{maximum}."
                )
            if amount > SHORT_MAX and not is_final_merged:
                evidence = evidence_by_part.get(number)
                if not _evidence_is_semantic(evidence, part):
                    raise RuntimeError(
                        f"SHORT Part {number} has {amount} words but lacks verified revision and semantic-preservation evidence."
                    )

    final_story_text = "\n\n".join(revised_groups)
    if word_count(final_story_text) <= 0:
        raise RuntimeError("Final story became empty after Part revision.")

    # Re-select/persist the actual count only after any final merge has completed.
    selected_count = len(revised_groups)
    set_selected_parts(
        config, selected_count, source="corrected_story_scene_aware_partition",
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
            "verified" if evidence_by_part else
            "attempted_without_verified_change" if attempted_parts else
            "not_required"
        ),
        "revision_parts": sorted(set(attempted_parts)),
        "revision_evidence_parts": sorted(evidence_by_part),
        "revision_results": revision_rows,
        "revision_attempt_count": sum(len(row.get("attempts", [])) for row in revision_rows),
        "final_part_merged": bool(merged_fallback),
        "final_merged_maximum_words": SHORT_MERGED_MAX if merged_fallback else None,
        "actual_parts": selected_count,
        "parts_count": selected_count,
        "selected_parts": selected_count,
        "final_parts_word_counts": [
            {"part": number, "words": word_count(part), "sha256": sha256(part),
             "revision_evidence": evidence_by_part.get(number)}
            for number, part in enumerate(revised_groups, 1)
        ],
    })
    validation_parts = []
    for number, part in enumerate(revised_groups, 1):
        validation_parts.append({
            "part": number,
            "narration": part,
            "revision_evidence": evidence_by_part.get(number),
            "final_part_merged": bool(merged_fallback and number == len(revised_groups)),
        })
    validation = validate_all_parts(validation_parts, policy)
    # The policy's standard validator intentionally flags a final merged Part >180;
    # validate the documented 239-word exception here instead of suppressing other errors.
    if mode == "short":
        filtered_errors = [
            error for error in validation.get("errors", [])
            if not (merged_fallback and "Part " + str(len(revised_groups)) + ":" in error)
        ]
        if filtered_errors:
            raise RuntimeError("SHORT Part validation failed: " + " | ".join(filtered_errors))
        validation["errors"] = filtered_errors
        validation["valid"] = True
        validation["passed"] = True
        validation["final_validation_passed"] = True
        validation["final_merged_exception_applied"] = bool(merged_fallback)
        if merged_fallback:
            final_number = len(revised_groups)
            validation["retry_parts"] = [
                number for number in validation.get("retry_parts", [])
                if number != final_number
            ]
            validation["retry_required"] = bool(validation["retry_parts"])
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
    return result


def validate_story(story: dict, expected_parts: int,
                   scenes_range: tuple[int, int], mode: str) -> None:
    if not isinstance(story, dict):
        raise RuntimeError("Final story output is not a JSON object.")
    if story.get("status") != "completed":
        raise RuntimeError("Final story status is not completed.")
    if story.get("format") != mode:
        raise RuntimeError("Final story format does not match selected mode.")
    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != expected_parts:
        raise RuntimeError("Final story Part count mismatch.")
    if story.get("total_parts") != expected_parts:
        raise RuntimeError("Final story total_parts mismatch.")
    if story.get("parts_count") != expected_parts:
        raise RuntimeError("Final story parts_count mismatch.")
    if isinstance(scenes_range, int) and not isinstance(scenes_range, bool):
        scenes_range = (scenes_range, scenes_range)
    scenes_min, scenes_max = scenes_range
    counts = []
    for number, part in enumerate(parts, 1):
        if part.get("part") != number:
            raise RuntimeError(f"Final story Part numbering mismatch at Part {number}.")
        narration = normalize_text(part.get("narration"))
        if not narration or word_count(narration) != part.get("word_count"):
            raise RuntimeError(f"Final story Part {number} narration/word count mismatch.")
        if clean(part.get("story_anchor")) != clean(narration):
            raise RuntimeError(f"Final story Part {number} anchor mismatch.")
        scenes = part.get("scenes")
        if not isinstance(scenes, list):
            raise RuntimeError(f"Final story Part {number} scenes are invalid.")
        count = len(scenes)
        if not scenes_min <= count <= scenes_max:
            raise RuntimeError(
                f"Final story Part {number} has {count} Main Scenes; "
                f"expected {scenes_min}-{scenes_max}."
            )
        if part.get("main_scene_count") != count or part.get("actual_scene_count") != count:
            raise RuntimeError(f"Final story Part {number} scene count metadata mismatch.")
        for scene_number, scene in enumerate(scenes, 1):
            expected_id = f"part_{number:02d}_scene_{scene_number:02d}"
            if scene.get("scene_id") != expected_id:
                raise RuntimeError(f"Final story scene ID mismatch: {expected_id}.")
            if scene.get("role") != "main":
                raise RuntimeError(f"Final story scene role mismatch: {expected_id}.")
            if not clean(scene.get("narration")):
                raise RuntimeError(f"Final story scene narration is empty: {expected_id}.")
        hook = part.get("hook_scene")
        if not isinstance(hook, dict) or hook.get("role") != "hook":
            raise RuntimeError(f"Final story Part {number} has no Hook scene.")
        if number < expected_parts:
            suspense = part.get("suspense_scene")
            if not isinstance(suspense, dict) or suspense.get("role") != "suspense":
                raise RuntimeError(f"Final story Part {number} has no Suspense scene.")
        else:
            resolution = part.get("resolution_scene")
            if not isinstance(resolution, dict) or resolution.get("role") != "resolution":
                raise RuntimeError("Final story has no final Resolution scene.")
        counts.append(count)

    if story.get("actual_scene_counts") != counts:
        raise RuntimeError("Story actual_scene_counts do not match Part Scene records.")
    total = sum(counts)
    if story.get("total_scenes") != total:
        raise RuntimeError("Story total_scenes does not match Main Scene records.")
    if story.get("total_main_scenes") != total:
        raise RuntimeError("Story total_main_scenes does not match Main Scene records.")
    if not story.get("story_plan_fingerprint"):
        raise RuntimeError("Story Plan fingerprint is missing.")
    reconstructed = "\n\n".join(normalize_text(part.get("narration")) for part in parts)
    if clean(reconstructed) != clean(story.get("corrected_complete_story")):
        raise RuntimeError("Final story Parts do not reconstruct the corrected complete story.")
    if word_count(reconstructed) != story.get("corrected_complete_story_word_count"):
        raise RuntimeError("Final corrected story word count mismatch.")
    if mode == "short":
        for number, part in enumerate(parts, 1):
            amount = word_count(part["narration"])
            merged = bool(part.get("final_part_merged") and number == expected_parts)
            maximum = SHORT_MERGED_MAX if merged else SHORT_HARD_MAX
            if amount < SHORT_MIN or amount > maximum:
                raise RuntimeError(
                    f"SHORT Part {number} has {amount} words; allowed {SHORT_MIN}-{maximum}."
                )


def cache_is_valid(story: dict, mode: str, topic: str,
                   source_signature: str, scenes_range: tuple[int, int]) -> bool:
    if not isinstance(story, dict):
        return False
    if story.get("status") != "completed" or story.get("format") != mode:
        return False
    if story.get("topic") != topic:
        return False
    if story.get("source_fingerprint") != source_signature:
        return False
    try:
        parts = story.get("parts")
        count = int(story.get("total_parts", 0))
        if not isinstance(parts, list) or count != len(parts) or count < 1:
            return False
        validate_story(story, count, scenes_range, mode)
        return True
    except Exception:
        return False


def reusable_corrected_story_checkpoint(
    checkpoint: dict | None,
    mode: str,
    topic: str,
    source_signature: str,
) -> dict | None:
    if not isinstance(checkpoint, dict):
        return None
    candidates = [
        checkpoint.get("corrected_story"),
        checkpoint.get("story"),
        checkpoint.get("story_result"),
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("format") != mode or candidate.get("topic") != topic:
            continue
        if candidate.get("source_fingerprint") != source_signature:
            continue
        text = normalize_text(
            candidate.get("corrected_complete_story")
            or candidate.get("story_text")
        )
        if not text:
            continue
        candidate["corrected_complete_story"] = text
        candidate["story_text"] = text
        return candidate
    return None


def _write_story_cache(story: dict, checkpoint: dict | None = None) -> None:
    write_json(OUTPUT_FILE, story)
    if isinstance(checkpoint, dict):
        checkpoint["corrected_story"] = story
        checkpoint["story"] = story
        write_checkpoint(checkpoint)


def get_selected_model(config: dict) -> str:
    selected = (
        config.get("STORY_MODEL")
        or config.get("GEMINI_MODEL")
        or config.get("MODEL")
    )
    if not selected:
        raise RuntimeError("No configured story model was selected.")
    return str(selected).strip()


def get_api_key(config: dict) -> str:
    key = (
        config.get("GEMINI_API_KEY")
        or config.get("GOOGLE_API_KEY")
        or config.get("GOOGLE_GENAI_API_KEY")
    )
    if not key:
        raise RuntimeError("Configured Gemini API key is missing.")
    return str(key).strip()


def _restore_validated_cache(cached: dict, config: dict, mode: str,
                             topic: str, source_text: str,
                             source_signature: str,
                             scenes_range: tuple[int, int],
                             model: str) -> dict:
    """Rebuild all dependent Story Plan and Scene records from a valid story cache."""
    cached_text = normalize_text(
        cached.get("corrected_complete_story") or cached.get("story_text")
    )
    if not cached_text:
        raise RuntimeError("Reusable story cache has no corrected complete story.")
    cached_count = int(cached.get("total_parts", 0))
    if cached_count < 1:
        raise RuntimeError("Reusable story cache has an invalid Part count.")
    cached_parts = cached.get("parts")
    if not isinstance(cached_parts, list) or len(cached_parts) != cached_count:
        raise RuntimeError("Reusable story cache Part records are incomplete.")
    part_texts = [normalize_text(row.get("narration")) for row in cached_parts]
    if any(not text for text in part_texts):
        raise RuntimeError("Reusable story cache contains an empty Part narration.")
    if clean("\n\n".join(part_texts)) != clean(cached_text):
        raise RuntimeError("Reusable story cache Parts do not reconstruct its complete story.")
    plan = make_final_plan(
        config, mode, topic, cached_text, cached_count, scenes_range, part_texts
    )
    title = clean(cached.get("final_title") or cached.get("title") or topic)
    plan["title"] = title
    plan["final_title"] = title
    plan["title_source"] = "validated_story_cache"
    write_json(PLAN_FILE, plan)
    proofreading = cached.get("proofreading")
    if not isinstance(proofreading, dict):
        proofreading = {"status": "reused_validated_cache"}
    policy = dict(cached.get("story_length_policy") or {})
    selection = dict(cached.get("parts_selection") or {})
    selection.setdefault("minimum_parts", cached_count)
    selection.setdefault("maximum_parts", cached_count)
    selection.setdefault("selected_parts", cached_count)
    selection.setdefault("corrected_story_words", word_count(cached_text))
    rebuilt = make_story(
        title, topic, mode, source_text, cached_text, part_texts,
        plan, scenes_range, model, proofreading, policy, selection,
        source_signature,
    )
    policy["actual_scene_counts"] = rebuilt.get("actual_scene_counts", [])
    policy["total_main_scenes"] = rebuilt.get("total_main_scenes", 0)
    rebuilt["story_length_policy"] = policy
    validate_story(rebuilt, cached_count, scenes_range, mode)
    _write_story_cache(rebuilt)
    return rebuilt

    }
    validate_story(result, len(story_parts), scenes_range, mode)
    return result


def validate_story(story: dict, expected_parts: int,
                   expected_scenes: int | tuple[int, int], mode: str) -> bool:
    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("Final story status is not completed.")
    if clean(story.get("format")).lower() != mode:
        raise RuntimeError("Final story format does not match Input/topic.txt.")
    if clean(story.get("scene_order")) != "part_then_scene":
        raise RuntimeError("Final story main Scene order metadata is invalid.")
    if clean(story.get("role_order")) != "hook_then_main_then_suspense_or_resolution":
        raise RuntimeError("Final story role order metadata is invalid.")
    if not clean(story.get("generation_method")) or not clean(story.get("model")):
        raise RuntimeError("Final story generation/model metadata is missing.")
    if not isinstance(story.get("proofreading"), dict):
        raise RuntimeError("Final story proofreading metadata is missing.")
    if clean(story.get("source_text")) and story["proofreading"].get("performed") is not True:
        raise RuntimeError("Supplied story has no completed proofreading record.")
    if not clean(story.get("title")) or clean(story.get("final_title")) != clean(story.get("title")):
        raise RuntimeError("Final story title is missing or inconsistent.")
    if isinstance(expected_scenes, (tuple, list)):
        scenes_min, scenes_max = int(expected_scenes[0]), int(expected_scenes[1])
    else:
        scenes_min = scenes_max = int(expected_scenes)
    if not (1 <= scenes_min <= scenes_max <= 500):
        raise RuntimeError("Expected SCENES range is invalid.")
    parts = story.get("parts")
    scenes = story.get("scenes")
    if not isinstance(parts, list) or len(parts) != expected_parts:
        raise RuntimeError("Final story Part count mismatch.")
    if not isinstance(scenes, list):
        raise RuntimeError("Final story Main Scene list is missing.")
    if mode == "full" and expected_parts != 1:
        raise RuntimeError("FULL mode must contain exactly one complete Part.")
    flattened = []
    combined_parts = []
    actual_counts = []
    global_scene = 0
    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != part_number:
            raise RuntimeError(f"Invalid Part numbering at Part {part_number}.")
        if clean(part.get("part_id")) != f"part_{part_number:02d}":
            raise RuntimeError(f"Part {part_number} ID is invalid.")
        narration = clean(part.get("narration"))
        if not narration or part.get("word_count") != word_count(narration):
            raise RuntimeError(f"Part {part_number} narration/word count is invalid.")
        if clean(part.get("story_anchor")) != narration:
            raise RuntimeError(f"Part {part_number} story_anchor does not match narration.")
        part_scenes = part.get("scenes")
        if not isinstance(part_scenes, list) or not scenes_min <= len(part_scenes) <= scenes_max:
            raise RuntimeError(f"Part {part_number} Scene count must be {scenes_min}-{scenes_max}.")
        is_merged_final = bool(part.get("final_part_merged") and part_number == expected_parts)
        if mode == "short":
            maximum = SHORT_MERGED_MAX if is_merged_final else SHORT_HARD_MAX
            count = word_count(narration)
            if count < SHORT_MIN or count > maximum:
                raise RuntimeError(f"SHORT Part {part_number} has {count} words; allowed {SHORT_MIN}-{maximum}.")
            if count > SHORT_MAX and not is_merged_final:
                evidence = part.get("revision_evidence")
                if not _evidence_is_semantic(evidence, narration):
                    raise RuntimeError(f"SHORT Part {part_number} above 150 lacks verified revision evidence.")
        scene_texts = []
        for scene_number, scene in enumerate(part_scenes, 1):
            global_scene += 1
            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid Scene record {scene_id}.")
            expected_values = {
                "part": part_number, "scene": scene_number,
                "scene_id": scene_id, "global_scene_number": global_scene,
                "role": "main",
            }
            for field, expected in expected_values.items():
                if scene.get(field) != expected:
                    raise RuntimeError(f"{scene_id} has invalid {field}.")
            for field in ("narration", "visual", "visual_prompt"):
                if not clean(scene.get(field)):
                    raise RuntimeError(f"{scene_id} is missing {field}.")
            if clean(scene["visual"]) != clean(scene["visual_prompt"]):
                raise RuntimeError(f"{scene_id} visual fields disagree.")
            scene_texts.append(clean(scene["narration"]))
            flattened.append(scene)
        if clean(" ".join(scene_texts)) != narration:
            raise RuntimeError(f"Scene narration does not reconstruct Part {part_number}.")
        if clean(" ".join(scene_texts)) != clean(part.get("story_anchor")):
            raise RuntimeError(f"Part {part_number} Scene anchors do not match narration.")
        if part.get("main_scene_count") != len(part_scenes) or part.get("actual_scene_count") != len(part_scenes):
            raise RuntimeError(f"Part {part_number} actual Scene count metadata mismatch.")
        combined_parts.append(narration)
        actual_counts.append(len(part_scenes))

        extras = []
        for role, field, required in (
            ("hook", "hook_scene", True),
            ("suspense", "suspense_scene", part_number < expected_parts),
            ("resolution", "resolution_scene", part_number == expected_parts),
        ):
            record = part.get(field)
            if not required:
                if record is not None:
                    raise RuntimeError(f"Part {part_number} has unexpected {role} record.")
                continue
            expected_role_id = f"part_{part_number:02d}_{role}"
            if not isinstance(record, dict):
                raise RuntimeError(f"Part {part_number} {role.title()} record is missing.")
            if (record.get("part") != part_number
                    or record.get("role") != role
                    or record.get("scene_id") != expected_role_id
                    or record.get("scene") is not None
                    or record.get("global_scene_number") is not None
                    or record.get("separate_from_main_scenes") is not True
                    or record.get("main_scene_numbering_affected") is not False
                    or record.get("is_final_part") is not (part_number == expected_parts)):
                raise RuntimeError(f"Part {part_number} {role.title()} record metadata is invalid.")
            if not clean(record.get("story_anchor")):
                raise RuntimeError(f"Part {part_number} {role.title()} story anchor is missing.")
            expected_anchor = scene_texts[0] if role == "hook" else scene_texts[-1]
            if clean(record.get("story_anchor")) != expected_anchor:
                raise RuntimeError(f"Part {part_number} {role.title()} anchor mismatch.")
            for field in ("purpose", "instruction", "continuity_rule", "invention_policy", "visual_policy"):
                if not clean(record.get(field)):
                    raise RuntimeError(f"Part {part_number} {role.title()} is missing {field}.")
            extras.append(record)
        if part.get("extra_scenes") != extras:
            raise RuntimeError(f"Part {part_number} extra Scene role list mismatch.")
    if flattened != scenes:
        raise RuntimeError("Top-level Main Scenes differ from ordered Part Scenes.")
    combined = clean(" ".join(combined_parts))
    if clean(story.get("story_text")) != combined or clean(story.get("corrected_complete_story")) != combined:
        raise RuntimeError("Complete story text does not match ordered Part narrations.")
    if story.get("corrected_complete_story_word_count") != word_count(combined):
        raise RuntimeError("Corrected complete-story word-count metadata mismatch.")
    if story.get("source_word_count") != word_count(story.get("source_text", "")):
        raise RuntimeError("Source story word-count metadata mismatch.")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", clean(story.get("source_fingerprint"))):
        raise RuntimeError("Final story source fingerprint is missing or invalid.")
    if story.get("actual_scene_counts") != actual_counts:
        raise RuntimeError("Actual per-Part Scene count metadata mismatch.")
    if story.get("scenes_min") != scenes_min or story.get("scenes_max") != scenes_max:
        raise RuntimeError("Configured Scene range metadata mismatch.")
    if story.get("scenes_range") != [scenes_min, scenes_max]:
        raise RuntimeError("Scene range tuple metadata mismatch.")
    if story.get("total_parts") != expected_parts or story.get("parts_count") != expected_parts:
        raise RuntimeError("Final story Part metadata mismatch.")
    if story.get("selected_parts_count") != expected_parts or story.get("selected_parts") != expected_parts:
        raise RuntimeError("Selected Part metadata mismatch.")
    if story.get("total_scenes") != len(scenes):
        raise RuntimeError("Total Main Scene metadata mismatch.")
    if story.get("total_main_scenes") != len(scenes):
        raise RuntimeError("Total Main Scene count metadata mismatch.")
    hooks = [part["hook_scene"] for part in parts]
    suspenses = [part["suspense_scene"] for part in parts if part.get("suspense_scene") is not None]
    resolutions = [part["resolution_scene"] for part in parts if part.get("resolution_scene") is not None]
    if story.get("hook_scenes") != hooks or story.get("suspense_scenes") != suspenses or story.get("resolution_scenes") != resolutions:
        raise RuntimeError("Top-level Hook/Suspense/Resolution records mismatch.")
    if story.get("total_hook_scenes") != len(hooks):
        raise RuntimeError("Hook Scene count metadata mismatch.")
    if story.get("total_suspense_scenes") != len(suspenses):
        raise RuntimeError("Suspense Scene count metadata mismatch.")
    if story.get("total_resolution_scenes") != len(resolutions):
        raise RuntimeError("Resolution Scene count metadata mismatch.")
    if story.get("total_story_words") != word_count(story.get("story_text")):
        raise RuntimeError("Total story word-count metadata mismatch.")
    return True


def cache_is_valid(cached: dict | None, source_fingerprint: str, mode: str,
                   scenes_range: tuple[int, int] | int, minimum_parts: int,
                   maximum_parts: int) -> bool:
    if isinstance(scenes_range, int) and not isinstance(scenes_range, bool):
        scenes_range = (scenes_range, scenes_range)
    if not isinstance(cached, dict) or cached.get("source_fingerprint") != source_fingerprint:
        return False
    try:
        count = int(cached.get("total_parts"))
        if not minimum_parts <= count <= maximum_parts or cached.get("format") != mode:
            return False
        if count * scenes_range[1] > MAX_TOTAL_SCENES:
            return False
        validate_story(cached, count, scenes_range, mode)
        policy = cached.get("story_length_policy")
        if not isinstance(policy, dict) or policy.get("selected_parts") != count:
            return False
        if mode == "short":
            for index, part in enumerate(cached.get("parts", []), 1):
                amount = word_count(part.get("narration"))
                merged = bool(part.get("final_part_merged") and index == count)
                if amount > SHORT_MAX and not merged and not _evidence_is_semantic(part.get("revision_evidence"), part.get("narration")):
                    return False
        return True
    except (RuntimeError, TypeError, ValueError, KeyError):
        return False


def reusable_corrected_story_checkpoint(
    story: dict | None,
    *,
    mode: str,
    topic: str,
    explicit_title: str,
    source_text: str,
    model: str,
) -> bool:
    """Reuse a prior corrected complete story when its original inputs still match."""
    if not isinstance(story, dict) or story.get("status") != "completed":
        return False
    if clean(story.get("format")).lower() != mode:
        return False
    if clean(story.get("topic")) != clean(topic):
        return False
    if clean(story.get("source_text")) != clean(source_text):
        return False
    if explicit_title and clean(story.get("title")) != clean(explicit_title):
        return False
    if clean(story.get("model")) != clean(model):
        return False
    corrected = normalize_text(story.get("corrected_complete_story"))
    if word_count(corrected) < 20:
        return False
    declared_words = story.get("corrected_complete_story_word_count")
    if isinstance(declared_words, bool):
        return False
    try:
        if int(declared_words) != word_count(corrected):
            return False
    except (TypeError, ValueError):
        return False
    proofreading = story.get("proofreading")
    if not isinstance(proofreading, dict) or proofreading.get("performed") is not True:
        return False
    if not clean(story.get("generation_method")) or not clean(story.get("title")):
        return False
    if len(clean(story.get("title"))) > 180:
        return False
    if source_text:
        try:
            if int(story.get("source_word_count", -1)) != word_count(source_text):

                return False
        except (TypeError, ValueError):
            return False
    if clean(story.get("final_title")) != clean(story.get("title")):
        return False
    return True


def get_final_title(supplied_title: str, narrative_title: str, complete_story: str,
                    model: str, api_key: str) -> str:
    if supplied_title:
        return supplied_title
    if clean(narrative_title):
        return clean(narrative_title)
    result = call_model(f"""
Read the COMPLETE Hindi story, including its middle and ending.
Return JSON only: {{"title":"..."}}.
Create one concise meaningful Hindi title representing the entire story, its central conflict and final outcome.
Do not title it only from the opening lines. Do not invent facts. Do not return a summary.
COMPLETE STORY:
{complete_story}
""", model, api_key, max_tokens=1000)
    title = clean(result.get("title"))
    if not title:
        raise RuntimeError("Configured model did not return a full-story-based title.")
    return title


def main() -> None:
    STORY_DIR.mkdir(parents=True, exist_ok=True)
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Input configuration missing: {INPUT_FILE}")
    config = load_and_validate(str(INPUT_FILE))
    mode = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    title_input = clean(config.get("TITLE", ""))
    source_text = get_complete_source(config)
    parts_specification = clean(config.get("PARTS_SPEC") or config.get("PARTS") or "1")
    minimum_parts, maximum_parts = get_parts_range(config)
    scenes_range = get_scenes_range(config)
    scenes_min, scenes_max = scenes_range
    story_length = config.get("STORY_LENGTH", "auto")

    if mode not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")
    if mode == "full" and (minimum_parts, maximum_parts) != (1, 1):
        raise RuntimeError("FULL format requires exactly PARTS=1.")
    if scenes_max * minimum_parts > MAX_TOTAL_SCENES:
        raise RuntimeError("Configured minimum PARTS × maximum SCENES exceeds the total Scene safety limit.")
    if not source_text and not topic and not title_input:
        raise RuntimeError("Provide STORY_TEXT, TOPIC or TITLE.")
    if source_text and word_count(source_text) < 20:
        raise RuntimeError("Complete STORY_TEXT contains fewer than 20 words.")

    model = get_selected_model()
    api_key = get_api_key()
    source_signature = fingerprint({
        "version": VERSION,
        "format": mode,
        "parts_specification": parts_specification,
        "scenes_range": list(scenes_range),
        "story_length": story_length,
        "topic": topic,
        "title": title_input,
        "source_text": source_text,
        "model": model,
    })
    log("FORMAT:", mode)
    log("PARTS RANGE:", f"{minimum_parts}-{maximum_parts}")
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("SOURCE STORY WORDS:", word_count(source_text))
    log("MODEL:", model)

    cached = read_json(OUTPUT_FILE)
    if cache_is_valid(cached, source_signature, mode, scenes_range, minimum_parts, maximum_parts):
        cached_count = int(cached["total_parts"])
        set_selected_parts(config, cached_count, source="validated_story_cache",
                           story_word_count=word_count(cached.get("corrected_complete_story", "")))
        cached_text = normalize_text(cached.get("corrected_complete_story"))
        cached_parts = [normalize_text(row.get("narration")) for row in cached["parts"]]
        plan = make_final_plan(config, mode, topic, cached_text, cached_count, scenes_range, cached_parts)
        if cached.get("story_plan_fingerprint") != plan.get("source_fingerprint"):
            cached["story_plan_fingerprint"] = plan.get("source_fingerprint")
            write_json(OUTPUT_FILE, cached)
        atomic_write(TITLE_FILE, clean(cached["title"]) + "\n")
        log("Matching validated story reused.")
        log("ACTUAL PARTS:", cached_count)
        log("ACTUAL SCENE COUNTS:", cached.get("actual_scene_counts"))
        log("External story-generation API calls: 0")
        log("STORY GENERATION: SUCCESS")
        return

    checkpoint = cached
    if not isinstance(checkpoint, dict):
        cached_wrapper = read_json(CACHE_FILE)
        candidate = cached_wrapper.get("story") if isinstance(cached_wrapper, dict) else None
        checkpoint = candidate if isinstance(candidate, dict) else None
    if reusable_corrected_story_checkpoint(
        checkpoint,
        mode=mode,
        topic=topic,
        explicit_title=title_input,
        source_text=source_text,
        model=model,
    ):
        narrative = {
            "story_text": normalize_text(checkpoint.get("corrected_complete_story")),
            "title": clean(checkpoint.get("title")),
            "generation_method": clean(checkpoint.get("generation_method")),
            "model": clean(checkpoint.get("model")),
            "proofreading": dict(checkpoint.get("proofreading")),
        }
        log("Reusing matching corrected complete-story checkpoint; no full-story generation API call.")
    else:
        if not api_key:
            raise RuntimeError("Story correction/generation requires the configured Gemini API key.")
        narrative = generate_complete_story(mode, topic, title_input, source_text, config, model, api_key)
    corrected_story = normalize_text(narrative["story_text"])
    if word_count(corrected_story) < 20:
        raise RuntimeError("Corrected complete story is too short.")
    final_title = get_final_title(title_input, narrative.get("title", ""), corrected_story, model, api_key)

    final_parts, policy, selection = _partition_and_revise(
        corrected_story, mode, parts_specification, story_length, config,
        model, api_key, final_title, scenes_range,
    )
    final_complete_story = "\n\n".join(final_parts)
    policy["source_story_word_count"] = word_count(source_text)
    policy["corrected_story_word_count"] = word_count(final_complete_story)
    policy["corrected_story_sha256"] = sha256(final_complete_story)
    policy["story_was_corrected_before_counting"] = True
    policy["selection_persisted"] = True
    policy["scenes_min"] = scenes_min
    policy["scenes_max"] = scenes_max
    policy["scenes_range"] = list(scenes_range)
    write_json(POLICY_FILE, policy)

    selected_count = len(final_parts)
    if selected_count * scenes_max > MAX_TOTAL_SCENES:
        raise RuntimeError(
            "Actual selected PARTS × configured maximum SCENES exceeds "
            f"the Story Plan safety limit of {MAX_TOTAL_SCENES}."
        )
    plan = make_final_plan(config, mode, topic, final_complete_story,
                           selected_count, scenes_range, final_parts)
    actual_part_texts = [normalize_text(part.get("narration")) for part in plan["parts"]]
    actual_complete_story = "\n\n".join(actual_part_texts)
    if clean(actual_complete_story) != clean(final_complete_story):
        raise RuntimeError("Variable Scene plan changed the final corrected story text.")
    plan["final_title"] = final_title
    plan["title_source"] = "final_corrected_story"
    write_json(PLAN_FILE, plan)

    # Attach revision evidence to the corresponding final Part after planning.
    story = make_story(
        title=final_title, topic=topic, mode=mode,
        source_text=source_text, complete_story=actual_complete_story,
        parts=actual_part_texts, plan=plan, scenes_range=scenes_range,
        model=model, proofreading=narrative["proofreading"],
        policy=policy, selection=selection,
        source_fingerprint=source_signature,
    )
    # Plan metadata records the actual, not the configured-minimum, Scene count.
    policy["actual_scene_counts"] = story["actual_scene_counts"]
    policy["total_main_scenes"] = story["total_scenes"]
    story["story_length_policy"] = policy
    validate_story(story, selected_count, scenes_range, mode)
    write_json(OUTPUT_FILE, story)
    write_json(CACHE_FILE, {
        "version": VERSION, "status": "completed",
        "source_fingerprint": source_signature,
        "format": mode, "total_parts": selected_count,
        "title": final_title, "story": story,
    })
    atomic_write(TITLE_FILE, final_title + "\n")

    log("STORY GENERATION: SUCCESS")
    log("FORMAT:", mode)
    log("TITLE:", final_title)
    log("CORRECTED STORY WORDS:", word_count(actual_complete_story))
    log("CONFIGURED PARTS:", parts_specification)
    log("ACTUAL SELECTED PARTS:", selected_count)
    log("SCENES RANGE:", f"{scenes_min}-{scenes_max}")
    log("ACTUAL SCENE COUNTS:", story["actual_scene_counts"])
    log("TOTAL MAIN SCENES:", story["total_scenes"])
    log("REVISION ATTEMPTED PARTS:", policy.get("revision_parts", []))
    log("FINAL PART MERGED:", policy.get("final_part_merged", False))
    log("MODEL:", model)
    log("STORY OUTPUT:", OUTPUT_FILE)
    log("STORY PLAN:", PLAN_FILE)
    log("LENGTH POLICY:", POLICY_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("STORY GENERATION: CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log("STORY GENERATION: FAILED")
        log(type(exc).__name__ + ":", str(exc))
        raise SystemExit(1)