#!/usr/bin/env python3
"""Correct the complete story, select and persist actual PARTS, then build the final story plan."""

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

from input_config import (
    load_and_validate,
    get_format,
    get_topic,
    get_story_text,
    get_parts_range,
    get_scenes,
    set_selected_parts,
)
from story_length_policy import (
    parse_parts_range,
    build_length_policy,
    select_final_part_count,
    update_policy_with_corrected_story,
    validate_all_parts,
)
from generate_story_plan import (
    build_plan as build_story_plan,
    validate_plan as validate_story_plan,
)

STORY_DIR = ROOT / "output" / "story"
INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT_FILE = STORY_DIR / "story.json"
CACHE_FILE = STORY_DIR / "story_cache.json"
PLAN_FILE = STORY_DIR / "story_plan.json"
TITLE_FILE = STORY_DIR / "final_title.txt"
POLICY_FILE = STORY_DIR / "story_length_policy.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

VERSION = "story-builder-v30-selection-and-revision-fix"
SHORT_MIN = 120
SHORT_TARGET = 135
SHORT_MAX = 150
SHORT_HARD_MAX = 180
REVISION_ATTEMPTS = 2
MAX_TOKENS = 16000
TIMEOUT = 180
RETRY_DELAYS = (5, 12)
MAX_PARTS = 1000
MAX_SCENES = 1000


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
    return hashlib.sha256(
        str(value or "").encode("utf-8")
    ).hexdigest()


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
        with temporary.open(
            "w", encoding="utf-8", newline="\n"
        ) as handle:
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
        value = json.loads(
            path.read_text(encoding="utf-8-sig")
        )
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def read_complete_story_assignment(path: Path) -> str:
    """Read a complete multiline STORY_TEXT assignment."""
    if not path.is_file():
        return ""

    lines = path.read_text(
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
        delimiter = (
            '"""' if value.startswith('"""')
            else "'''" if value.startswith("'''")
            else None
        )

        if delimiter:
            remainder = value[len(delimiter):]
            closing = remainder.find(delimiter)

            if closing >= 0:
                trailing = remainder[
                    closing + len(delimiter):
                ].strip()

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

                    trailing = current[
                        closing + len(delimiter):
                    ].strip()

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
    parsed = normalize_text(get_story_text(config))
    direct = normalize_text(
        read_complete_story_assignment(INPUT_FILE)
    )

    if word_count(direct) > word_count(parsed):
        log("Using complete direct STORY_TEXT assignment.")
        return direct

    return parsed or direct


def split_sentences(text: str) -> list[str]:
    result = []

    for block in re.split(r"\n+", normalize_text(text)):
        if not block.strip():
            continue

        result.extend(
            clean(piece)
            for piece in re.split(
                r"(?<=[।॥.!?！？…])\s+",
                block,
            )
            if clean(piece)
        )

    return result


def split_clauses(sentence: str) -> list[str]:
    return [
        clean(piece)
        for piece in re.split(
            r"(?<=[,，;:—–])\s+",
            clean(sentence),
        )
        if clean(piece)
    ]


def split_balanced(text: str, count: int) -> list[str]:
    """
    Split chronologically at sentence boundaries first.
    Use comma/clause boundaries only when necessary.
    Never cut at arbitrary word positions.
    """
    if count < 1:
        raise RuntimeError("Part/scene count must be positive.")

    sentences = split_sentences(text)

    if not sentences:
        raise RuntimeError(
            "Story contains no complete sentence."
        )

    units = list(sentences)

    if len(units) < count:
        units = []
        for sentence in sentences:
            units.extend(split_clauses(sentence))

    if len(units) < count:
        raise RuntimeError(
            f"Only {len(units)} natural sentence/clause boundaries "
            f"exist for {count} requested groups. "
            "Mid-sentence cutting is forbidden."
        )

    groups = []
    cursor = 0
    remaining_words = sum(word_count(x) for x in units)

    for index in range(count):
        remaining_groups = count - index
        remaining_units = len(units) - cursor

        if remaining_groups == 1:
            groups.append(" ".join(units[cursor:]))
            cursor = len(units)
            break

        target = remaining_words / remaining_groups
        max_take = remaining_units - remaining_groups + 1
        selected = []
        selected_words = 0

        while cursor < len(units) and len(selected) < max_take:
            candidate = units[cursor]
            candidate_words = word_count(candidate)

            if selected and abs(selected_words - target) <= abs(
                selected_words + candidate_words - target
            ):
                break

            selected.append(candidate)
            selected_words += candidate_words
            cursor += 1

            if selected_words >= target:
                break

        if not selected:
            raise RuntimeError(
                "Unable to create a natural non-empty group."
            )

        groups.append(" ".join(selected))
        remaining_words -= selected_words

    if cursor != len(units) or len(groups) != count:
        raise RuntimeError(
            "Natural segmentation did not produce the exact group count."
        )

    original = clean(" ".join(units))
    reconstructed = clean(" ".join(groups))

    if original != reconstructed:
        raise RuntimeError(
            "Segmentation lost, duplicated or reordered story material."
        )

    return groups


def get_selected_model() -> str:
    data = read_json(MODEL_FILE)
    model = clean(data.get("model")) if data else ""

    if not model or not re.fullmatch(
        r"[A-Za-z0-9._-]+", model
    ):
        raise RuntimeError(
            "Configured selected Gemini model is missing or invalid."
        )

    return model


def get_api_key() -> str:
    return (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )


class ModelError(RuntimeError):
    pass


def parse_model_response(raw: str) -> dict:
    value = str(raw or "").strip()

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
            "temperature": 0.25,
            "maxOutputTokens": max_tokens,
        },
    }

    errors = []

    for attempt in range(2):
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
                request, timeout=TIMEOUT
            ) as response:
                response_data = json.loads(
                    response.read().decode(
                        "utf-8", errors="replace"
                    )
                )

            candidates = response_data.get("candidates") or []

            if not candidates:
                raise ModelError(
                    "Configured model returned no candidate."
                )

            response_parts = (
                candidates[0]
                .get("content", {})
                .get("parts", [])
            )

            response_text = "".join(
                item.get("text", "")
                for item in response_parts
                if isinstance(item, dict)
            ).strip()

            if not response_text:
                raise ModelError(
                    "Configured model returned empty text."
                )

            return parse_model_response(response_text)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8", errors="replace"
            )
            errors.append(
                f"HTTP {exc.code}: {body[:1200]}"
            )

            if exc.code not in {
                408, 429, 500, 502, 503, 504
            }:
                break

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            ModelError,
        ) as exc:
            errors.append(str(exc))

            if isinstance(exc, ModelError):
                break

        if attempt == 0:
            time.sleep(RETRY_DELAYS[0])

    raise RuntimeError(
        f"Configured model {model} failed: "
        + " | ".join(errors)
    )


def build_story_prompt(
    mode: str,
    topic: str,
    title: str,
    source_text: str,
    config: dict,
) -> str:
    task = (
        "Correct and proofread the complete supplied story."
        if source_text
        else "Write a complete original Hindi story from the topic/title."
    )

    return f"""
You are a professional Hindi story editor.

TASK: {task}
FORMAT: {mode}
TOPIC: {topic or "(not supplied)"}
EXPLICIT TITLE: {title or "(not supplied)"}
AUDIENCE: {config.get("AUDIENCE", "adult")}

Read the COMPLETE story from beginning to ending before editing.

SOURCE STORY:
<source_story>
{source_text}
</source_story>

MANDATORY RULES:
1. Correct Hindi matras, spelling, grammar and punctuation.
2. Correct sentence construction without changing meaning.
3. Preserve every character, relationship, location, object, clue,
   event, motivation, chronology and consequence.
4. Preserve the original ending and all meaningful events.
5. Do not invent events or remove meaningful story material.
6. Use natural Hindi Devanagari unless the source requires otherwise.
7. Return the entire corrected story, not a summary.
8. If TITLE is blank, derive it after reading the complete story,
   including its middle and ending.
9. Do not derive the title only from the opening lines.
10. Return JSON with title and story_text. Plain text is accepted.
11. Do not create an alternate story or second format.

Return:
{{
  "title": "full-story-based title",
  "story_text": "complete corrected story"
}}
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
    if not api_key:
        if source_text:
            raise RuntimeError(
                "Offline mode cannot proofread the supplied story. "
                "Correction was not performed; generation stopped."
            )

        raise RuntimeError(
            "Topic/title-only story generation requires the configured API key."
        )

    result = call_model(
        build_story_prompt(
            mode, topic, title, source_text, config
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

    if not complete:
        raw_parts = result.get("parts", [])

        if isinstance(raw_parts, list):
            complete = normalize_text(
                "\n\n".join(
                    str(
                        row.get("text", "")
                        if isinstance(row, dict)
                        else row
                    )
                    for row in raw_parts
                )
            )

    if not complete or word_count(complete) < 20:
        raise RuntimeError(
            "Model did not return a complete usable story."
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
        },
    }


def repair_short_part(
    part_text: str,
    part_number: int,
    complete_story: str,
    title: str,
    model: str,
    api_key: str,
) -> tuple[str, list[dict]]:
    current = part_text
    attempts = []

    for attempt in range(1, REVISION_ATTEMPTS + 1):
        count = word_count(current)

        if SHORT_MIN <= count <= SHORT_MAX:
            break

        direction = (
            "Reduce the part toward 120–150 words."
            if count > SHORT_MAX
            else "Expand the part toward 120–150 words using only "
                 "relevant details already present in the complete story."
        )

        prompt = f"""
Revise this Hindi narration part.
Return JSON only: {{"text":"..."}}

PART: {part_number}
TITLE: {title}
CURRENT WORD COUNT: {count}
TARGET: 120–150 words.
HARD LIMIT: 180 words.

COMPLETE STORY CONTEXT:
{complete_story}

CURRENT PART:
{current}

RULES:
- {direction}
- Preserve every event, character, relationship, chronology and consequence.
- Do not invent events, clues, dialogue, twists or outcomes.
- Do not remove an important event.
- Correct Hindi matras, spelling, grammar and punctuation.
- Keep complete sentences and complete thoughts together.
- Never cut a sentence mid-way.
- Keep the revised part in the same chronological position.
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
                or response.get("story")
                or response.get("narration")
                or response.get("story_text")
            )

            if not revised:
                raise ModelError(
                    "Revision model returned empty narration."
                )

            record["after_words"] = word_count(revised)
            record["after_sha256"] = sha256(revised)
            record["outcome"] = (
                "within_target"
                if SHORT_MIN <= word_count(revised) <= SHORT_MAX
                else (
                    "within_hard_limit_after_attempt"
                    if SHORT_MIN <= word_count(revised) <= SHORT_HARD_MAX
                    else "outside_hard_limit"
                )
            )

            attempts.append(record)
            current = revised

            if SHORT_MIN <= word_count(current) <= SHORT_MAX:
                break

        except Exception as exc:
            record["outcome"] = "failed"
            record["error"] = str(exc)
            attempts.append(record)

    final_count = word_count(current)

    if not SHORT_MIN <= final_count <= SHORT_HARD_MAX:
        raise RuntimeError(
            f"SHORT part {part_number} remains invalid after "
            f"{len(attempts)} revision attempts: {final_count} words. "
            "Allowed final range is 120–180."
        )

    if final_count > SHORT_MAX and not attempts:
        raise RuntimeError(
            f"SHORT part {part_number} has {final_count} words; "
            "a revision attempt is mandatory above 150."
        )

    return current, attempts


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
    total_words = word_count(complete_story)
    minimum, maximum = parse_parts_range(
        parts_specification
    )

    selected_count = select_final_part_count(
        total_corrected_words=total_words,
        format_type=mode,
        parts_specification=parts_specification,
        story_length=story_length,
        config=None,
    )

    # Persist the actual selected count for BOTH SHORT and FULL.
    # A range minimum is never treated as the final selected count.
    set_selected_parts(
        config,
        selected_count,
        source="corrected_complete_story",
        story_word_count=total_words,
    )

    policy = build_length_policy(
        format_type=mode,
        story_length=story_length,
        parts=f"{minimum}-{maximum}",
        scenes_per_part=get_scenes(config),
        scene_duration=config.get("SCENE_DURATION", "auto"),
        source_word_count=total_words,
        selected_parts=selected_count,
    )

    policy["corrected_story_word_count"] = total_words
    policy["corrected_story_sha256"] = sha256(
        complete_story
    )
    policy["story_was_corrected_before_counting"] = True
    policy["selection_persisted"] = True
    policy["selection_source"] = "corrected_complete_story"

    original_parts = split_balanced(
        complete_story,
        selected_count,
    )

    parts = []
    revision_records = []
    attempted_part_numbers = []

    if mode == "short":
        for number, original in enumerate(
            original_parts, 1
        ):
            count = word_count(original)

            if SHORT_MIN <= count <= SHORT_MAX:
                parts.append(original)
                revision_records.append({
                    "part": number,
                    "attempted": False,
                    "outcome": "already_within_target",
                    "before_words": count,
                    "after_words": count,
                    "before_sha256": sha256(original),
                    "after_sha256": sha256(original),
                    "attempts": [],
                })
                continue

            if not api_key:
                raise RuntimeError(
                    f"Offline mode cannot revise SHORT part {number} "
                    f"({count} words). No revision is claimed."
                )

            attempted_part_numbers.append(number)

            revised, attempts = repair_short_part(
                original,
                number,
                complete_story,
                title,
                model,
                api_key,
            )

            parts.append(revised)
            revision_records.append({
                "part": number,
                "attempted": True,
                "outcome": (
                    "within_target"
                    if SHORT_MIN <= word_count(revised) <= SHORT_MAX
                    else "accepted_after_revision_within_hard_limit"
                ),
                "before_words": count,
                "after_words": word_count(revised),
                "before_sha256": sha256(original),
                "after_sha256": sha256(revised),
                "attempts": attempts,
            })

    else:
        parts = list(original_parts)

    if len(parts) != selected_count:
        raise RuntimeError(
            "Final selected part count does not match the persisted selection."
        )

    for number, part in enumerate(parts, 1):
        count = word_count(part)

        if not part:
            raise RuntimeError(f"Part {number} is empty.")

        if mode == "short":
            if count < SHORT_MIN or count > SHORT_HARD_MAX:
                raise RuntimeError(
                    f"SHORT part {number} has {count} words; "
                    "allowed final range is 120–180."
                )

            if count > SHORT_MAX and number not in attempted_part_numbers:
                raise RuntimeError(
                    f"SHORT part {number} has {count} words; "
                    "revision is mandatory above 150."
                )

    # The final story is reconstructed from the exact final part narrations.
    # This keeps story.json, Story Plan and all downstream narration aligned.
    final_story_text = "\n\n".join(parts)

    policy = update_policy_with_corrected_story(
        policy,
        final_story_text,
        selected_parts=selected_count,
        config=None,
    )

    validation_parts = [
        {
            "part": number,
            "narration": part,
            "revision_attempted": number in attempted_part_numbers,
        }
        for number, part in enumerate(parts, 1)
    ]

    validation = validate_all_parts(
        validation_parts,
        policy,
    )

    if not validation.get("valid"):
        raise RuntimeError(
            "Final story part validation failed: "
            + " | ".join(validation.get("errors", []))
        )

    policy["revision_attempted"] = bool(
        attempted_part_numbers
    )
    policy["revision_outcome"] = (
        "completed" if attempted_part_numbers else "not_required"
    )
    policy["revision_parts"] = attempted_part_numbers
    policy["revision_results"] = revision_records
    policy["revision_attempt_count"] = sum(
        len(row.get("attempts", []))
        for row in revision_records
    )
    policy["parts_validation"] = validation
    policy["parts_count"] = selected_count
    policy["selected_parts"] = selected_count
    policy["actual_parts"] = len(parts)
    policy["corrected_story_word_count"] = word_count(
        final_story_text
    )
    policy["corrected_story_sha256"] = sha256(
        final_story_text
    )
    policy["final_parts_word_counts"] = [
        {
            "part": number,
            "words": word_count(part),
            "sha256": sha256(part),
        }
        for number, part in enumerate(parts, 1)
    ]

    selection = {
        "selected_parts": selected_count,
        "minimum_parts": minimum,
        "maximum_parts": maximum,
        "corrected_story_words": total_words,
        "final_story_words": word_count(final_story_text),
        "revision_attempted_parts": attempted_part_numbers,
    }

    return parts, policy, selection

def make_final_plan(
    config: dict,
    mode: str,
    topic: str,
    complete_story: str,
    parts_count: int,
    scenes_per_part: int,
) -> dict:
    """Build the final plan only after correction and actual PARTS selection."""
    plan = build_story_plan(
        config=config,
        format_type=mode,
        topic=topic,
        story_text=complete_story,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
        dynamic_short=False,
    )

    validate_story_plan(
        plan,
        mode,
        topic,
        parts_count,
        scenes_per_part,
    )

    source = plan.get("source")

    if not isinstance(source, dict):
        raise RuntimeError(
            "Final Story Plan source metadata is missing."
        )

    if source.get("story_word_count") != word_count(
        complete_story
    ):
        raise RuntimeError(
            "Final Story Plan was not built from the final story."
        )

    if plan.get("parts_count") != parts_count:
        raise RuntimeError(
            "Final Story Plan has an incorrect part count."
        )

    if plan.get("source_fingerprint") != fingerprint(
        plan.get("source_payload", {})
    ):
        raise RuntimeError(
            "Final Story Plan source fingerprint mismatch."
        )

    write_json(PLAN_FILE, plan)
    return plan


def make_story(
    title: str,
    topic: str,
    mode: str,
    source_text: str,
    complete_story: str,
    parts: list[str],
    plan: dict,
    scenes_per_part: int,
    model: str,
    proofreading: dict,
    policy: dict,
    selection: dict,
    source_fingerprint: str,
) -> dict:
    plan_parts = plan.get("parts")

    if (
        not isinstance(plan_parts, list)
        or len(plan_parts) != len(parts)
    ):
        raise RuntimeError(
            "Final plan and final story part counts do not match."
        )

    story_parts = []
    all_scenes = []
    global_scene = 0

    for part_number, narration in enumerate(parts, 1):
        plan_part = plan_parts[part_number - 1]
        planned_scenes = plan_part.get("scenes")

        if (
            not isinstance(planned_scenes, list)
            or len(planned_scenes) != scenes_per_part
        ):
            raise RuntimeError(
                f"Plan main scene count mismatch for part {part_number}."
            )

        scene_narrations = split_balanced(
            narration,
            scenes_per_part,
        )

        if clean(" ".join(scene_narrations)) != clean(narration):
            raise RuntimeError(
                f"Scene segmentation changed narration in part {part_number}."
            )

        scenes = []

        for scene_number, scene_text in enumerate(
            scene_narrations, 1
        ):
            global_scene += 1
            planned = planned_scenes[scene_number - 1]

            if not isinstance(planned, dict):
                raise RuntimeError(
                    "Invalid final Story Plan scene record."
                )

            expected_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            if planned.get("scene_id") != expected_id:
                raise RuntimeError(
                    f"Story Plan scene ID mismatch: {expected_id}."
                )

            anchor = clean(planned.get("story_anchor"))
            purpose = clean(planned.get("purpose"))
            visual = clean(
                planned.get("visual_prompt")
                or planned.get("visual")
                or planned.get("description")
            )

            if not visual:
                visual = (
                    "Photorealistic cinematic live-action depiction "
                    "of the exact narrated moment: "
                    f"{scene_text}. Story anchor: {anchor}. "
                    "Show only story-supported characters, objects, "
                    "locations and actions. Maintain identity, "
                    "chronology and continuity."
                )

            scene = {
                "part": part_number,
                "scene": scene_number,
                "scene_id": expected_id,
                "global_scene_number": global_scene,
                "role": "main",
                "beat": clean(planned.get("beat")),
                "purpose": purpose,
                "story_anchor": anchor,
                "narration": scene_text,
                "visual": visual,
                "visual_prompt": visual,
                "dialogue": "",
                "suspense": purpose,
            }

            scenes.append(scene)
            all_scenes.append(scene)

        story_parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": f"भाग {part_number}",
            "narration": narration,
            "word_count": word_count(narration),
            "revision_attempted": (
                part_number in policy.get("revision_parts", [])
            ),
            "scenes": scenes,
        })

    final_story_text = "\n\n".join(
        row["narration"] for row in story_parts
    )

    if clean(final_story_text) != clean(complete_story):
        raise RuntimeError(
            "Final part narrations do not reconstruct corrected_complete_story."
        )

    result = {
        "version": VERSION,
        "status": "completed",
        "title": title,
        "final_title": title,
        "topic": topic,
        "format": mode,
        "model": model,
        "generation_method": "configured_gemini",
        "proofreading": proofreading,
        "source_text": source_text,
        "source_word_count": word_count(source_text),
        "corrected_complete_story": complete_story,
        "corrected_complete_story_word_count": word_count(
            complete_story
        ),
        "story_text": final_story_text,
        "total_story_words": word_count(final_story_text),
        "total_parts": len(parts),
        "parts_count": len(parts),
        "selected_parts": len(parts),
        "selected_parts_count": len(parts),
        "configured_parts_specification": (
            f"{selection['minimum_parts']}-{selection['maximum_parts']}"
            if selection["minimum_parts"] != selection["maximum_parts"]
            else str(selection["minimum_parts"])
        ),
        "configured_parts_min": selection["minimum_parts"],
        "configured_parts_max": selection["maximum_parts"],
        "scenes_per_part": scenes_per_part,
        "total_scenes": len(all_scenes),
        "scene_order": "part_then_scene",
        "parts": story_parts,
        "scenes": all_scenes,
        "story_plan_fingerprint": plan.get(
            "source_fingerprint"
        ),
        "story_length_policy": policy,
        "parts_selection": selection,
        "source_fingerprint": source_fingerprint,
    }

    validate_story(
        result,
        len(parts),
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
    if (
        not isinstance(story, dict)
        or story.get("status") != "completed"
    ):
        raise RuntimeError(
            "Final story status is not completed."
        )

    if clean(story.get("format")).lower() != mode:
        raise RuntimeError(
            "Final story format does not match Input/topic.txt."
        )

    if not clean(story.get("title")):
        raise RuntimeError("Final story title is missing.")

    parts = story.get("parts")
    scenes = story.get("scenes")

    if (
        not isinstance(parts, list)
        or len(parts) != expected_parts
    ):
        raise RuntimeError(
            "Final story part count mismatch."
        )

    if not isinstance(scenes, list):
        raise RuntimeError(
            "Final story scene list is missing."
        )

    if len(scenes) != expected_parts * expected_scenes:
        raise RuntimeError(
            "Final story scene count mismatch."
        )

    flattened = []
    combined_parts = []
    global_scene = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(
                f"Invalid story part {part_number}."
            )

        if part.get("part") != part_number:
            raise RuntimeError(
                f"Incorrect part numbering at {part_number}."
            )

        narration = clean(part.get("narration"))

        if not narration:
            raise RuntimeError(
                f"Part {part_number} narration is empty."
            )

        count = word_count(narration)

        if part.get("word_count") != count:
            raise RuntimeError(
                f"Part {part_number} word count metadata mismatch."
            )

        if mode == "short":
            if count < SHORT_MIN or count > SHORT_HARD_MAX:
                raise RuntimeError(
                    f"SHORT part {part_number} has {count} words; "
                    "expected 120–180."
                )

            if (
                count > SHORT_MAX
                and not part.get("revision_attempted")
            ):
                raise RuntimeError(
                    f"SHORT part {part_number} has {count} words "
                    "without a recorded revision attempt."
                )

        part_scenes = part.get("scenes")

        if (
            not isinstance(part_scenes, list)
            or len(part_scenes) != expected_scenes
        ):
            raise RuntimeError(
                f"Part {part_number} scene count mismatch."
            )

        scene_texts = []

        for scene_number, scene in enumerate(
            part_scenes, 1
        ):
            global_scene += 1

            expected_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Invalid scene record {expected_id}."
                )

            expected_values = {
                "part": part_number,
                "scene": scene_number,
                "scene_id": expected_id,
                "global_scene_number": global_scene,
                "role": "main",
            }

            for key, expected in expected_values.items():
                if scene.get(key) != expected:
                    raise RuntimeError(
                        f"{expected_id} has invalid {key}."
                    )

            for field in (
                "narration",
                "visual",
                "visual_prompt",
            ):
                if not clean(scene.get(field)):
                    raise RuntimeError(
                        f"{expected_id} is missing {field}."
                    )

            if clean(scene["visual"]) != clean(
                scene["visual_prompt"]
            ):
                raise RuntimeError(
                    f"{expected_id} visual fields disagree."
                )

            scene_texts.append(clean(scene["narration"]))
            flattened.append(scene)

        if clean(" ".join(scene_texts)) != narration:
            raise RuntimeError(
                f"Scene narration does not reconstruct part {part_number}."
            )

        combined_parts.append(narration)

    if flattened != scenes:
        raise RuntimeError(
            "Top-level scenes differ from ordered part scenes."
        )

    combined = clean(" ".join(combined_parts))

    if clean(story.get("story_text")) != combined:
        raise RuntimeError(
            "Final story_text does not match its ordered parts."
        )

    if clean(story.get("corrected_complete_story")) != combined:
        raise RuntimeError(
            "Corrected complete story does not match final part narrations."
        )

    if story.get("total_parts") != expected_parts:
        raise RuntimeError(
            "Final total_parts metadata mismatch."
        )

    if story.get("parts_count") != expected_parts:
        raise RuntimeError(
            "Final parts_count metadata mismatch."
        )

    if story.get("selected_parts_count") != expected_parts:
        raise RuntimeError(
            "Final selected part count metadata mismatch."
        )

    if story.get("selected_parts") != expected_parts:
        raise RuntimeError(
            "Final selected_parts metadata mismatch."
        )

    if story.get("total_scenes") != (
        expected_parts * expected_scenes
    ):
        raise RuntimeError(
            "Final total_scenes metadata mismatch."
        )

    if story.get("total_story_words") != word_count(
        story.get("story_text")
    ):
        raise RuntimeError(
            "Final total story word count mismatch."
        )

    return True


def cache_is_valid(
    cached: dict | None,
    source_fingerprint: str,
    mode: str,
    scenes_per_part: int,
    minimum_parts: int,
    maximum_parts: int,
) -> bool:
    if not isinstance(cached, dict):
        return False

    if cached.get("source_fingerprint") != source_fingerprint:
        return False

    try:
        count = int(cached.get("total_parts"))

        if not minimum_parts <= count <= maximum_parts:
            return False

        if cached.get("format") != mode:
            return False

        validate_story(
            cached,
            count,
            scenes_per_part,
            mode,
        )

        policy = cached.get("story_length_policy")

        if not isinstance(policy, dict):
            return False

        if policy.get("selected_parts") != count:
            return False

        if policy.get("actual_parts") != count:
            return False

        if mode == "short":
            validation = validate_all_parts(
                cached.get("parts", []),
                policy,
            )

            if not validation.get("valid"):
                return False

        return True

    except (
        RuntimeError,
        TypeError,
        ValueError,
        KeyError,
    ):
        return False


def get_final_title(
    supplied_title: str,
    narrative_title: str,
    complete_story: str,
    model: str,
    api_key: str,
) -> str:
    if supplied_title:
        return supplied_title

    title = clean(narrative_title)

    if title:
        return title

    prompt = f"""
Read the COMPLETE Hindi story below, including its middle and ending.

Return JSON only:
{{"title":"..." }}

Create one concise, meaningful Hindi title representing:
- the complete story,
- its central conflict,
- its important characters or central event,
- and its final outcome.

Do not use only the opening lines.
Do not invent an event or outcome.
Do not return a summary.

COMPLETE STORY:
{complete_story}
"""

    result = call_model(
        prompt,
        model,
        api_key,
        max_tokens=1000,
    )

    title = clean(result.get("title"))

    if not title:
        raise RuntimeError(
            "Configured model did not return a full-story-based title."
        )

    return title


def main() -> None:
    STORY_DIR.mkdir(parents=True, exist_ok=True)

    if not INPUT_FILE.is_file():
        raise RuntimeError(
            f"Input configuration missing: {INPUT_FILE}"
        )

    config = load_and_validate(str(INPUT_FILE))

    mode = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    title_input = clean(config.get("TITLE", ""))
    source_text = get_complete_source(config)

    parts_specification = clean(
        config.get("PARTS_SPEC")
        or config.get("PARTS")
        or "1"
    )

    minimum, maximum = parse_parts_range(
        parts_specification
    )

    scenes_per_part = int(get_scenes(config))
    story_length = config.get("STORY_LENGTH", "auto")

    if mode not in {"short", "full"}:
        raise RuntimeError(
            "FORMAT must be short or full."
        )

    if scenes_per_part < 1:
        raise RuntimeError(
            "SCENES must be positive."
        )

    if maximum > MAX_PARTS:
        raise RuntimeError(
            f"PARTS cannot exceed {MAX_PARTS}."
        )

    if maximum * scenes_per_part > MAX_SCENES:
        raise RuntimeError(
            "Configured part/scene count exceeds the safety limit."
        )

    if not source_text and not topic and not title_input:
        raise RuntimeError(
            "Provide STORY_TEXT, TOPIC or TITLE."
        )

    if source_text and word_count(source_text) < 20:
        raise RuntimeError(
            "Complete STORY_TEXT contains fewer than 20 words."
        )

    api_key = get_api_key()

    if not api_key:
        if source_text:
            raise RuntimeError(
                "Offline mode cannot claim Hindi proofreading. "
                "The supplied story was not corrected; stopping."
            )

        raise RuntimeError(
            "Story generation requires the configured model API key."
        )

    model = get_selected_model()

    source_fingerprint = fingerprint({
        "version": VERSION,
        "format": mode,
        "parts_specification": parts_specification,
        "scenes_per_part": scenes_per_part,
        "story_length": story_length,
        "topic": topic,
        "title": title_input,
        "source_text": source_text,
        "model": model,
    })

    log("FORMAT:", mode)
    log("PARTS SPECIFICATION:", parts_specification)
    log("SOURCE STORY WORDS:", word_count(source_text))
    log("MODEL:", model)

    cached = read_json(OUTPUT_FILE)

    if cache_is_valid(
        cached,
        source_fingerprint,
        mode,
        scenes_per_part,
        minimum,
        maximum,
    ):
        cached_count = int(cached["total_parts"])

        # Re-persist selection if the matching story artifact exists
        # but the shared selection record was lost or restored separately.
        set_selected_parts(
            config,
            cached_count,
            source="validated_story_cache",
            story_word_count=word_count(
                cached.get("corrected_complete_story", "")
            ),
        )

        cached_story = clean(
            cached.get("corrected_complete_story")
        )

        if not cached_story:
            raise RuntimeError(
                "Cached story is missing corrected_complete_story."
            )

        plan = make_final_plan(
            config,
            mode,
            topic,
            cached_story,
            cached_count,
            scenes_per_part,
        )

        if (
            cached.get("story_plan_fingerprint")
            != plan.get("source_fingerprint")
        ):
            cached["story_plan_fingerprint"] = (
                plan.get("source_fingerprint")
            )
            write_json(OUTPUT_FILE, cached)

        atomic_write(
            TITLE_FILE,
            clean(cached["title"]) + "\n",
        )

        log("Matching validated story reused.")
        log("ACTUAL PARTS:", cached_count)
        log("STORY GENERATION: SUCCESS")
        return

    narrative = generate_complete_story(
        mode=mode,
        topic=topic,
        title=title_input,
        source_text=source_text,
        config=config,
        model=model,
        api_key=api_key,
    )

    corrected_story = normalize_text(
        narrative["story_text"]
    )

    if word_count(corrected_story) < 20:
        raise RuntimeError(
            "Corrected complete story is too short."
        )

    # Title is chosen only after the entire corrected story exists.
    final_title = get_final_title(
        title_input,
        narrative.get("title", ""),
        corrected_story,
        model,
        api_key,
    )

    final_parts, policy, selection = select_final_parts(
        complete_story=corrected_story,
        mode=mode,
        parts_specification=parts_specification,
        story_length=story_length,
        config=config,
        model=model,
        api_key=api_key,
        title=final_title,
    )

    final_complete_story = "\n\n".join(final_parts)

    policy["source_story_word_count"] = word_count(
        source_text
    )
    policy["corrected_story_word_count"] = word_count(
        final_complete_story
    )
    policy["corrected_story_sha256"] = sha256(
        final_complete_story
    )
    policy["story_was_corrected_before_counting"] = True
    policy["selection_persisted"] = True

    write_json(POLICY_FILE, policy)

    # The final plan is generated from the exact final narration,
    # after part revisions and actual PARTS persistence.
    plan = make_final_plan(
        config,
        mode,
        topic,
        final_complete_story,
        len(final_parts),
        scenes_per_part,
    )

    story = make_story(
        title=final_title,
        topic=topic,
        mode=mode,
        source_text=source_text,
        complete_story=final_complete_story,
        parts=final_parts,
        plan=plan,
        scenes_per_part=scenes_per_part,
        model=model,
        proofreading=narrative["proofreading"],
        policy=policy,
        selection=selection,
        source_fingerprint=source_fingerprint,
    )

    write_json(OUTPUT_FILE, story)

    write_json(CACHE_FILE, {
        "version": VERSION,
        "status": "completed",
        "source_fingerprint": source_fingerprint,
        "format": mode,
        "total_parts": len(final_parts),
        "title": final_title,
        "story": story,
    })

    atomic_write(
        TITLE_FILE,
        final_title + "\n",
    )

    log("STORY GENERATION: SUCCESS")
    log("FORMAT:", mode)
    log("TITLE:", final_title)
    log(
        "CORRECTED STORY WORDS:",
        word_count(final_complete_story),
    )
    log("CONFIGURED PARTS:", parts_specification)
    log("ACTUAL SELECTED PARTS:", len(final_parts))
    log(
        "REVISION ATTEMPTED PARTS:",
        policy.get("revision_parts", []),
    )
    log("SCENES PER PART:", scenes_per_part)
    log("TOTAL SCENES:", story["total_scenes"])
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
        raise