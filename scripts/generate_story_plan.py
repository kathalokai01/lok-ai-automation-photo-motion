#!/usr/bin/env python3
"""Katha Lok AI — story-grounded scene planning and validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from input_config import (
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_topic,
    get_story_text,
)

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / "Input" / "topic.txt"
OUTPUT_FILE = ROOT / "output" / "story" / "story_plan.json"

VERSION = "story-plan-v7-complete-anchor-coverage"

SHORT_MIN_WORDS = 120
SHORT_TARGET_WORDS = 135
SHORT_MAX_WORDS = 150
SHORT_HARD_MAX_WORDS = 180

SHORT_BEATS = (
    ("hook", "Open with immediate story-specific tension."),
    ("setup", "Establish the relevant character, setting and stakes."),
    ("discovery", "Introduce a clue or meaningful discovery."),
    ("escalation", "Increase pressure through established consequences."),
    ("obstacle", "Show a credible obstacle already supported by the story."),
    ("reversal", "Reveal a change in the established situation."),
    ("choice", "Show a consequential decision."),
    ("confrontation", "Bring the central conflict into focus."),
    ("reveal", "Reveal an established story fact."),
    ("climax", "Show the decisive turning point."),
    ("payoff", "Pay off an earlier clue, decision or consequence."),
    ("suspense", "End on a meaningful unresolved consequence."),
)

FULL_BEATS = (
    ("hook", "Open with strong story-specific tension."),
    ("establishing", "Establish setting, characters and stakes."),
    ("inciting_event", "Show the event that changes ordinary life."),
    ("goal", "Clarify the protagonist's established goal."),
    ("investigation", "Develop relevant clues and motivations."),
    ("first_obstacle", "Introduce a credible story-supported obstacle."),
    ("complication", "Raise the stakes through consequences."),
    ("relationship", "Develop relationships through story events."),
    ("discovery", "Reveal information that changes the situation."),
    ("midpoint", "Create a meaningful reversal."),
    ("pressure", "Increase urgency."),
    ("setback", "Deliver a believable setback."),
    ("reassessment", "Force a change of approach."),
    ("preparation", "Prepare for the established confrontation."),
    ("approach", "Move toward the central conflict."),
    ("confrontation", "Bring opposing goals into conflict."),
    ("crisis", "Force a consequential choice."),
    ("climax", "Show the decisive struggle."),
    ("aftermath", "Show the consequences."),
    ("resolution", "Provide narrative closure."),
    ("final_suspense", "Use suspense only when supported by the story."),
)

SENTENCE_ENDINGS = re.compile(r"(?<=[।॥.!?！？…])\s+")


def log(*values: Any, file=None) -> None:
    print(*values, file=file or sys.stdout, flush=True)


def clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", clean_text(text)))


def split_sentences(text: str) -> list[str]:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    result = []

    for block in re.split(r"\n+", text):
        block = block.strip()
        if not block:
            continue

        for item in SENTENCE_ENDINGS.split(block):
            item = clean_text(item)
            if item:
                result.append(item)

    return result


def fingerprint(data: Any) -> str:
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def as_bool(config: dict, key: str, default: bool = True) -> bool:
    value = config.get(key, default)

    if isinstance(value, bool):
        return value

    if isinstance(value, int) and value in (0, 1):
        return bool(value)

    value = str(value).strip().lower()

    if value in {"true", "yes", "on", "1", "enabled"}:
        return True

    if value in {"false", "no", "off", "0", "disabled", ""}:
        return False

    return default


def calculate_dynamic_short_parts(total_words: int) -> int:
    """Choose a feasible number of Shorts near 135 words each."""
    total_words = int(total_words)

    if total_words < SHORT_MIN_WORDS:
        raise RuntimeError(
            f"SHORT story has {total_words} words; "
            f"minimum is {SHORT_MIN_WORDS}."
        )

    minimum_parts = (
        total_words + SHORT_HARD_MAX_WORDS - 1
    ) // SHORT_HARD_MAX_WORDS

    maximum_parts = total_words // SHORT_MIN_WORDS

    if minimum_parts > maximum_parts:
        raise RuntimeError(
            f"{total_words} words cannot be divided into parts "
            f"of {SHORT_MIN_WORDS}–{SHORT_HARD_MAX_WORDS} words."
        )

    preferred = round(total_words / SHORT_TARGET_WORDS)

    return max(
        minimum_parts,
        min(preferred, maximum_parts),
    )


def _split_long_sentence(sentence: str, maximum: int) -> list[str]:
    """Split only when required; retain every original token in order."""
    tokens = re.findall(r"\S+", sentence)

    if len(tokens) <= maximum:
        return [sentence]

    result = []

    for start in range(0, len(tokens), maximum):
        chunk = " ".join(tokens[start:start + maximum]).strip()

        if not chunk:
            continue

        # Mark continuation chunks without changing their wording.
        result.append(chunk)

    return result


def _expand_sentence_units(sentences: list[str], maximum: int = 180):
    expanded = []

    for sentence in sentences:
        expanded.extend(_split_long_sentence(sentence, maximum))

    return expanded


def story_anchors(
    story_text: str,
    topic: str,
    total_scenes: int,
) -> list[str]:
    """
    Cover the complete supplied story in chronological order.

    Unlike sparse sentence sampling, this does not discard intermediate
    story sentences merely because the scene count is smaller.
    """
    if total_scenes < 1:
        raise RuntimeError("total_scenes must be positive.")

    sentences = _expand_sentence_units(
        split_sentences(story_text),
        maximum=180,
    )

    if not sentences:
        fallback = clean_text(topic)
        return [fallback] * total_scenes if fallback else [""] * total_scenes

    if len(sentences) <= total_scenes:
        anchors = list(sentences)
        anchors.extend([""] * (total_scenes - len(anchors)))
        return anchors

    weights = [max(1, word_count(item)) for item in sentences]
    total_words = sum(weights)
    anchors = []
    cursor = 0

    for scene_index in range(total_scenes):
        remaining_scenes = total_scenes - scene_index
        remaining_sentences = len(sentences) - cursor

        if remaining_scenes == 1:
            anchors.append(" ".join(sentences[cursor:]))
            break

        target_words = sum(weights[cursor:]) / remaining_scenes
        max_take = remaining_sentences - (remaining_scenes - 1)

        selected = []
        selected_words = 0

        while cursor < len(sentences) and len(selected) < max_take:
            next_words = weights[cursor]

            if (
                selected
                and abs(selected_words - target_words)
                <= abs(selected_words + next_words - target_words)
            ):
                break

            selected.append(sentences[cursor])
            selected_words += next_words
            cursor += 1

            if selected_words >= target_words:
                break

        if not selected:
            selected.append(sentences[cursor])
            cursor += 1

        anchors.append(" ".join(selected))

    if len(anchors) != total_scenes:
        raise RuntimeError("Could not distribute story anchors.")

    original = clean_text(" ".join(sentences))
    reconstructed = clean_text(" ".join(anchors))

    if original != reconstructed:
        raise RuntimeError(
            "Story anchor distribution lost, reordered or duplicated text."
        )

    return anchors


def select_beat(format_type: str, index: int, total: int):
    beats = SHORT_BEATS if format_type == "short" else FULL_BEATS

    if total <= 1:
        return beats[0]

    position = round(index * (len(beats) - 1) / (total - 1))
    position = max(0, min(position, len(beats) - 1))

    return beats[position]


def make_scene_purpose(
    role: str,
    description: str,
    anchor: str,
    topic: str,
) -> str:
    subject = clean_text(anchor) or clean_text(topic)

    if role == "part_opening":
        if subject:
            return (
                f"{description} Start from this established story detail: "
                f"{subject}. Do not invent events or characters."
            )
        return description

    if role == "part_ending":
        if subject:
            return (
                f"{description} Build suspense only from this established "
                f"story detail: {subject}. Do not fabricate a plot twist."
            )
        return description

    if role == "finale":
        if subject:
            return (
                f"{description} Connect the ending to established events: "
                f"{subject}. Do not invent an unsupported solution."
            )
        return description

    if subject:
        return (
            f"{description} Relevant chronological story material: "
            f"{subject}. Preserve the source chronology and causality."
        )

    return description


def build_plan(
    config: dict,
    format_type: str,
    topic: str,
    story_text: str,
    parts_count: int,
    scenes_per_part: int,
    dynamic_short: bool,
) -> dict:
    format_type = clean_text(format_type).lower()
    topic = clean_text(topic)
    story_text = clean_text(story_text)

    if format_type not in {"short", "full"}:
        raise RuntimeError("format_type must be short or full.")

    parts_count = int(parts_count)
    scenes_per_part = int(scenes_per_part)

    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive.")

    total = parts_count * scenes_per_part

    anchors = story_anchors(
        story_text=story_text,
        topic=topic,
        total_scenes=total,
    )

    part_hook = as_bool(config, "PART_HOOK", True)
    part_suspense = as_bool(config, "PART_SUSPENSE", True)
    final_resolution = as_bool(config, "FINAL_RESOLUTION", True)

    parts = []
    global_number = 0

    for part_number in range(1, parts_count + 1):
        scenes = []

        for scene_number in range(1, scenes_per_part + 1):
            anchor = anchors[global_number]
            beat, description = select_beat(
                format_type,
                global_number,
                total,
            )

            is_first = scene_number == 1
            is_last = scene_number == scenes_per_part
            is_final_part = part_number == parts_count
            is_final_scene = is_last and is_final_part

            if is_final_scene and final_resolution:
                role = "finale"
                beat = "finale"
                description = (
                    "Resolve the central conflict through consequences "
                    "already supported by the source story. Do not invent "
                    "a new event or unsupported solution."
                )

            elif is_first and (part_number == 1 or part_hook):
                role = "part_opening"
                beat = "hook" if part_number == 1 else "part_hook"
                description = (
                    "Open with a compelling story-specific hook. "
                    "Use established tension and preserve chronology."
                )

            elif is_last and part_suspense:
                role = "part_ending"
                beat = "suspense"
                description = (
                    "End on an unresolved consequence, discovery or "
                    "decision that genuinely exists in the source story."
                )

            else:
                role = "story_progression"

            purpose = make_scene_purpose(
                role=role,
                description=description,
                anchor=anchor,
                topic=topic,
            )

            scene_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            scenes.append({
                "scene": scene_number,
                "global_scene_number": global_number + 1,
                "scene_id": scene_id,
                "role": role,
                "beat": beat,
                "beat_description": description,
                "purpose": purpose,
                "story_anchor": anchor,
                "continuity_rule": (
                    "Keep character identity, age, face, body, clothing, "
                    "location, objects, chronology and physical continuity "
                    "consistent with the source story and character bible."
                ),
                "invention_policy": (
                    "Do not invent unsupported characters, relationships, "
                    "motives, locations, objects, events, dialogue or twists."
                ),
                "visual_policy": (
                    "Visuals must depict the exact narrated story moment. "
                    "Do not use unrelated stock scenes or generic imagery."
                ),
            })

            global_number += 1

        parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": f"भाग {part_number}",
            "scenes": scenes,
        })

    source_payload = {
        "script_version": VERSION,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total,
        "dynamic_short_parts": bool(dynamic_short),
        "anchors": anchors,
        "parts": parts,
    }

    return {
        "status": "planned",
        "script_version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "format": format_type,
        "topic": topic,
        "title": topic or "",
        "title_source": (
            "explicit_topic"
            if topic
            else "deferred_to_full_story_generation"
        ),
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total,
        "scene_order": "part_then_scene",
        "mapping_version": 4,
        "dynamic_short_parts": bool(dynamic_short),
        "source_fingerprint": fingerprint(source_payload),
        "source": {
            "input_file": "Input/topic.txt",
            "story_text_provided": bool(story_text),
            "story_word_count": word_count(story_text),
            "planning_mode": "offline_story_grounded",
            "api_calls": 0,
        },
        "parts": parts,
    }

def validate_plan(
    plan: dict,
    format_type: str,
    topic: str,
    parts_count: int,
    scenes_per_part: int,
) -> None:
    if not isinstance(plan, dict):
        raise RuntimeError("Story plan must be a JSON object.")

    format_type = clean_text(format_type).lower()
    topic = clean_text(topic)
    parts_count = int(parts_count)
    scenes_per_part = int(scenes_per_part)

    if format_type not in {"short", "full"}:
        raise RuntimeError("Invalid plan format.")

    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError("Plan part and scene counts must be positive.")

    expected_total = parts_count * scenes_per_part

    expected_values = {
        "status": "planned",
        "format": format_type,
        "topic": topic,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_total,
        "scene_order": "part_then_scene",
        "mapping_version": 4,
    }

    for key, expected in expected_values.items():
        actual = plan.get(key)

        if actual != expected:
            raise RuntimeError(
                f"Story plan mismatch for {key}: "
                f"expected {expected!r}, got {actual!r}."
            )

    if clean_text(plan.get("title")) != topic:
        raise RuntimeError(
            "Story plan title must be the explicit topic or empty "
            "until the complete story is generated."
        )

    parts = plan.get("parts")

    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(
            f"Story plan must contain exactly {parts_count} parts."
        )

    global_number = 0
    all_anchors = []

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid plan part {part_number}.")

        if part.get("part") != part_number:
            raise RuntimeError(
                f"Incorrect part numbering at part {part_number}."
            )

        expected_part_id = f"part_{part_number:02d}"

        if clean_text(part.get("part_id")) != expected_part_id:
            raise RuntimeError(
                f"Incorrect part ID: {expected_part_id}."
            )

        scenes = part.get("scenes")

        if (
            not isinstance(scenes, list)
            or len(scenes) != scenes_per_part
        ):
            raise RuntimeError(
                f"Part {part_number} must contain exactly "
                f"{scenes_per_part} scenes."
            )

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1

            expected_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid scene: {expected_id}.")

            expected_scene_values = {
                "scene": scene_number,
                "global_scene_number": global_number,
                "scene_id": expected_id,
            }

            for field, expected in expected_scene_values.items():
                if scene.get(field) != expected:
                    raise RuntimeError(
                        f"Incorrect {field} in {expected_id}."
                    )

            for field in (
                "role",
                "beat",
                "beat_description",
                "purpose",
                "continuity_rule",
                "invention_policy",
                "visual_policy",
            ):
                if not clean_text(scene.get(field)):
                    raise RuntimeError(
                        f"Missing {field} in {expected_id}."
                    )

            anchor = scene.get("story_anchor", "")

            if not isinstance(anchor, str):
                raise RuntimeError(
                    f"Invalid story anchor in {expected_id}."
                )

            all_anchors.append(anchor)

    if global_number != expected_total:
        raise RuntimeError("Story plan global scene count is incorrect.")

    if format_type == "short":
        for part_number, part in enumerate(parts, 1):
            first_role = clean_text(
                part["scenes"][0].get("role")
            )

            if as_bool({"PART_HOOK": True}, "PART_HOOK"):
                if first_role != "part_opening":
                    raise RuntimeError(
                        f"SHORT part {part_number} is missing its opening hook."
                    )

            last_role = clean_text(
                part["scenes"][-1].get("role")
            )

            is_final_part = part_number == parts_count
            final_resolution = as_bool(
                {"FINAL_RESOLUTION": True},
                "FINAL_RESOLUTION",
            )

            if is_final_part and final_resolution:
                if last_role != "finale":
                    raise RuntimeError(
                        "Final SHORT part must end with a finale."
                    )
            elif last_role != "part_ending":
                raise RuntimeError(
                    f"SHORT part {part_number} is missing its ending suspense."
                )

    if not re.fullmatch(
        r"[a-fA-F0-9]{64}",
        clean_text(plan.get("source_fingerprint")),
    ):
        raise RuntimeError("Story plan source_fingerprint is invalid.")

    source = plan.get("source")

    if not isinstance(source, dict):
        raise RuntimeError("Story plan source metadata is missing.")

    if source.get("api_calls") != 0:
        raise RuntimeError(
            "Offline story planning must not make external API calls."
        )


def read_story_text_direct(path: Path) -> str:
    """
    Independently read the complete STORY_TEXT block.
    Supports both triple-double and triple-single quoted blocks.
    """
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

        delimiter = None

        if value.startswith('"""'):
            delimiter = '"""'
        elif value.startswith("'''"):
            delimiter = "'''"

        if delimiter:
            remainder = value[len(delimiter):]
            closing = remainder.find(delimiter)

            if closing >= 0:
                return remainder[:closing].strip()

            collected = [remainder] if remainder else []

            for next_index in range(index + 1, len(lines)):
                current = lines[next_index]
                closing = current.find(delimiter)

                if closing >= 0:
                    if current[:closing]:
                        collected.append(current[:closing])

                    trailing = current[closing + len(delimiter):].strip()

                    if trailing and not trailing.startswith("#"):
                        raise RuntimeError(
                            "Unexpected text after STORY_TEXT closing delimiter."
                        )

                    return "\n".join(collected).strip()

                collected.append(current)

            raise RuntimeError(
                "STORY_TEXT multiline block has no closing delimiter."
            )

        if len(value) >= 2 and value[0] == value[-1]:
            if value[0] in {"'", '"'}:
                return value[1:-1].strip()

        return value

    return ""


def get_complete_input_story(config: dict, path: Path) -> str:
    parsed = clean_text(get_story_text(config))
    direct = clean_text(read_story_text_direct(path))

    parsed_words = word_count(parsed)
    direct_words = word_count(direct)

    if direct_words > parsed_words:
        log(
            "STORY_TEXT parser mismatch:",
            f"parsed={parsed_words}",
            f"direct={direct_words}",
        )
        log("Using complete direct STORY_TEXT input.")
        return direct

    if parsed and direct and parsed_words != direct_words:
        log(
            "STORY_TEXT word-count difference:",
            f"parsed={parsed_words}",
            f"direct={direct_words}",
        )

    return parsed or direct


def main() -> None:
    if not INPUT_FILE.is_file():
        raise RuntimeError(
            f"Input file is missing: {INPUT_FILE}"
        )

    config = load_and_validate(str(INPUT_FILE))

    format_type = clean_text(get_format(config)).lower()
    topic = clean_text(get_topic(config))
    story_text = get_complete_input_story(config, INPUT_FILE)

    configured_parts = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if format_type not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT: {format_type}."
        )

    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError(
            "PARTS and SCENES must be positive integers."
        )

    if not topic and not story_text:
        raise RuntimeError(
            "Input/topic.txt must contain TOPIC or STORY_TEXT."
        )

    if story_text and word_count(story_text) < 20:
        raise RuntimeError(
            "STORY_TEXT contains fewer than 20 words. "
            "Check the complete multiline input."
        )

    if format_type == "short":
        parts_count = configured_parts
        dynamic_short = True

        log(
            "SHORT provisional plan:",
            parts_count,
            "configured parts.",
        )
        log(
            "Final Shorts count will be determined after "
            "the complete story rewrite."
        )
    else:
        parts_count = configured_parts
        dynamic_short = False

    plan = build_plan(
        config=config,
        format_type=format_type,
        topic=topic,
        story_text=story_text,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
        dynamic_short=dynamic_short,
    )

    validate_plan(
        plan,
        format_type,
        topic,
        parts_count,
        scenes_per_part,
    )

    old_plan = read_json(OUTPUT_FILE)

    if (
        isinstance(old_plan, dict)
        and old_plan.get("source_fingerprint")
        == plan.get("source_fingerprint")
        and old_plan.get("script_version") == VERSION
    ):
        try:
            validate_plan(
                old_plan,
                format_type,
                topic,
                parts_count,
                scenes_per_part,
            )

            log("Matching valid story-plan checkpoint preserved.")
            log("External API calls: 0")
            return

        except (RuntimeError, TypeError, ValueError) as exc:
            log("Existing plan is invalid; rebuilding:", exc)

    write_json(OUTPUT_FILE, plan)

    anchor_count = sum(
        bool(clean_text(scene.get("story_anchor")))
        for part in plan["parts"]
        for scene in part["scenes"]
    )

    log("STORY PLAN: SUCCESS")
    log("Version:", VERSION)
    log("Format:", format_type)
    log("Topic:", topic or "(story text only)")
    log("Source story words:", word_count(story_text))
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", parts_count * scenes_per_part)
    log("Story-grounded scenes:", anchor_count)
    log("Dynamic SHORT:", dynamic_short)
    log("Output:", OUTPUT_FILE)
    log("External API calls: 0")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log("STORY PLAN: FAILED")
        log(type(exc).__name__ + ":", str(exc), file=sys.stderr)
        sys.exit(1)