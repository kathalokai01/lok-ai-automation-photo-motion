#!/usr/bin/env python3
"""Katha Lok AI — deterministic, story-grounded scene planning."""

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

VERSION = "story-plan-v8-grounded-fixed-count"
MAPPING_VERSION = 4

SHORT_BEATS = (
    ("hook", "Open with immediate story-specific tension."),
    ("setup", "Establish the relevant character, setting and stakes."),
    ("discovery", "Introduce a meaningful clue or discovery."),
    ("escalation", "Increase pressure through established consequences."),
    ("obstacle", "Show a credible obstacle supported by the story."),
    ("reversal", "Reveal a change in the established situation."),
    ("choice", "Show a consequential decision."),
    ("confrontation", "Bring the central conflict into focus."),
    ("reveal", "Reveal an established story fact."),
    ("climax", "Show the decisive turning point."),
    ("payoff", "Pay off an earlier clue or consequence."),
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


def word_count(value: str) -> int:
    return len(re.findall(r"\S+", clean_text(value)))


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


def split_sentences(text: str) -> list[str]:
    """Split at paragraph and sentence boundaries without changing wording."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    result = []

    for block in re.split(r"\n+", text):
        block = block.strip()
        if not block:
            continue

        for sentence in SENTENCE_ENDINGS.split(block):
            sentence = clean_text(sentence)
            if sentence:
                result.append(sentence)

    return result


def _split_long_sentence(sentence: str, maximum: int = 180) -> list[str]:
    """Split exceptionally long units while retaining every token in order."""
    tokens = sentence.split()

    if len(tokens) <= maximum:
        return [sentence]

    return [
        " ".join(tokens[index:index + maximum])
        for index in range(0, len(tokens), maximum)
    ]


def story_anchors(
    story_text: str,
    topic: str,
    total_scenes: int,
) -> list[str]:
    """
    Distribute the complete story over scenes in original chronological order.
    No sentence is sampled out or silently discarded.
    """
    if total_scenes < 1:
        raise RuntimeError("Total scene count must be positive.")

    sentences = []

    for sentence in split_sentences(story_text):
        sentences.extend(_split_long_sentence(sentence))

    if not sentences:
        fallback = clean_text(topic)
        if not fallback:
            raise RuntimeError(
                "Cannot create story anchors: both story text and topic are empty."
            )
        return [fallback] * total_scenes

    if len(sentences) <= total_scenes:
        return sentences + [""] * (total_scenes - len(sentences))

    weights = [max(1, word_count(sentence)) for sentence in sentences]
    remaining_words = sum(weights)
    cursor = 0
    anchors = []

    for scene_index in range(total_scenes):
        remaining_scenes = total_scenes - scene_index
        remaining_units = len(sentences) - cursor

        if remaining_scenes == 1:
            anchors.append(" ".join(sentences[cursor:]))
            cursor = len(sentences)
            break

        target = remaining_words / remaining_scenes
        max_take = remaining_units - (remaining_scenes - 1)

        selected = []
        selected_words = 0

        while cursor < len(sentences) and len(selected) < max_take:
            next_weight = weights[cursor]

            if selected and abs(selected_words - target) <= abs(
                selected_words + next_weight - target
            ):
                break

            selected.append(sentences[cursor])
            selected_words += next_weight
            cursor += 1

            if selected_words >= target:
                break

        if not selected:
            selected.append(sentences[cursor])
            selected_words += weights[cursor]
            cursor += 1

        anchors.append(" ".join(selected))
        remaining_words -= selected_words

    if len(anchors) != total_scenes or cursor != len(sentences):
        raise RuntimeError("Story anchor distribution is incomplete.")

    original = clean_text(" ".join(sentences))
    reconstructed = clean_text(" ".join(anchors))

    if original != reconstructed:
        raise RuntimeError(
            "Story anchor integrity failed: text was lost, reordered or duplicated."
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
        return (
            f"{description} Start from this established story detail: "
            f"{subject}. Do not invent events or characters."
            if subject else description
        )

    if role == "part_ending":
        return (
            f"{description} Build suspense only from this established "
            f"story detail: {subject}. Do not fabricate a plot twist."
            if subject else description
        )

    if role == "finale":
        return (
            f"{description} Connect the ending to established events: "
            f"{subject}. Do not invent an unsupported solution."
            if subject else description
        )

    return (
        f"{description} Relevant chronological story material: {subject}. "
        "Preserve source chronology and causality."
        if subject else description
    )


def build_plan(
    config: dict,
    format_type: str,
    topic: str,
    story_text: str,
    parts_count: int,
    scenes_per_part: int,
    dynamic_short: bool = False,
) -> dict:
    """
    Build the exact configured part/scene structure.

    dynamic_short is retained for backward compatibility with existing callers.
    It is metadata only: this planner never silently changes configured PARTS.
    """
    format_type = clean_text(format_type).lower()
    topic = clean_text(topic)
    story_text = clean_text(story_text)

    if format_type not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")

    try:
        parts_count = int(parts_count)
        scenes_per_part = int(scenes_per_part)
    except (TypeError, ValueError):
        raise RuntimeError("PARTS and SCENES must be integers.")

    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")

    total_scenes = parts_count * scenes_per_part

    if total_scenes > 1000:
        raise RuntimeError("Requested plan exceeds the 1000-scene safety limit.")

    if not topic and not story_text:
        raise RuntimeError("A topic or complete story is required for planning.")

    anchors = story_anchors(story_text, topic, total_scenes)

    # Each SHORT part gets a hook and an ending. The final part gets a
    # resolution instead of an artificial cliffhanger when enabled.
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
                total_scenes,
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
                role,
                description,
                anchor,
                topic,
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
            "hook_required": bool(part_hook),
            "suspense_required": bool(
                part_suspense and not (
                    part_number == parts_count and final_resolution
                )
            ),
            "final_resolution": bool(
                part_number == parts_count and final_resolution
            ),
            "scenes": scenes,
        })

    source_payload = {
        "script_version": VERSION,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total_scenes,
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
            "explicit_topic" if topic
            else "deferred_to_full_story_generation"
        ),
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total_scenes,
        "scene_order": "part_then_scene",
        "mapping_version": MAPPING_VERSION,
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
    """Strictly validate the plan schema consumed by later pipeline stages."""
    if not isinstance(plan, dict):
        raise RuntimeError("Story plan must be a JSON object.")

    format_type = clean_text(format_type).lower()
    topic = clean_text(topic)

    try:
        parts_count = int(parts_count)
        scenes_per_part = int(scenes_per_part)
    except (TypeError, ValueError):
        raise RuntimeError("Expected part and scene counts must be integers.")

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
        "mapping_version": MAPPING_VERSION,
    }

    for key, expected in expected_values.items():
        if plan.get(key) != expected:
            raise RuntimeError(
                f"Story plan mismatch for {key}: "
                f"expected {expected!r}, got {plan.get(key)!r}."
            )

    if clean_text(plan.get("title")) != topic:
        raise RuntimeError("Story plan title does not match the configured topic.")

    if not re.fullmatch(
        r"[a-fA-F0-9]{64}",
        clean_text(plan.get("source_fingerprint")),
    ):
        raise RuntimeError("Story plan source_fingerprint is invalid.")

    source = plan.get("source")
    if not isinstance(source, dict):
        raise RuntimeError("Story plan source metadata is missing.")

    if source.get("api_calls") != 0:
        raise RuntimeError("Story planning must not make external API calls.")

    if source.get("planning_mode") != "offline_story_grounded":
        raise RuntimeError("Unexpected story planning mode.")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(
            f"Story plan must contain exactly {parts_count} parts."
        )

    global_number = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid plan part {part_number}.")

        if part.get("part") != part_number:
            raise RuntimeError(f"Incorrect part numbering at part {part_number}.")

        expected_part_id = f"part_{part_number:02d}"
        if clean_text(part.get("part_id")) != expected_part_id:
            raise RuntimeError(f"Incorrect part ID: {expected_part_id}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
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

            required_values = {
                "scene": scene_number,
                "global_scene_number": global_number,
                "scene_id": expected_id,
            }

            for field, expected in required_values.items():
                if scene.get(field) != expected:
                    raise RuntimeError(
                        f"Incorrect {field} in {expected_id}: "
                        f"expected {expected!r}, got {scene.get(field)!r}."
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
                        f"Missing required {field} in {expected_id}."
                    )

            anchor = scene.get("story_anchor", "")
            if not isinstance(anchor, str):
                raise RuntimeError(
                    f"Invalid story_anchor in {expected_id}."
                )

        # A one-scene SHORT part cannot simultaneously have separate
        # opening and ending scenes. Reject this impossible configuration.
        if format_type == "short" and scenes_per_part < 2:
            raise RuntimeError(
                "SHORT format requires at least 2 scenes per part "
                "to provide both an opening and an ending."
            )

        if format_type == "short":
            first_role = clean_text(scenes[0].get("role"))
            last_role = clean_text(scenes[-1].get("role"))
            is_final_part = part_number == parts_count

            if first_role != "part_opening":
                raise RuntimeError(
                    f"SHORT part {part_number} is missing its opening hook."
                )

            if is_final_part and part.get("final_resolution", True):
                if last_role != "finale":
                    raise RuntimeError(
                        "Final SHORT part must end with a finale."
                    )
            elif part.get("suspense_required", True):
                if last_role != "part_ending":
                    raise RuntimeError(
                        f"SHORT part {part_number} is missing ending suspense."
                    )

    if global_number != expected_total:
        raise RuntimeError("Story plan global scene count is incorrect.")


def read_story_text_direct(path: Path) -> str:
    """
    Read the complete multiline STORY_TEXT block independently of the
    configuration parser, supporting triple-double and triple-single quotes.
    """
    if not path.is_file():
        return ""

    lines = path.read_text(encoding="utf-8-sig").splitlines()

    for index, line in enumerate(lines):
        match = re.match(r"^\s*STORY_TEXT\s*=\s*(.*?)\s*$", line)
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
                trailing = remainder[closing + len(delimiter):].strip()
                if trailing and not trailing.startswith("#"):
                    raise RuntimeError(
                        "Unexpected content after STORY_TEXT closing delimiter."
                    )
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
                            "Unexpected content after STORY_TEXT closing delimiter."
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
    """Prefer the longer direct parse if the config parser truncated input."""
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
        raise RuntimeError(f"Input file is missing: {INPUT_FILE}")

    config = load_and_validate(str(INPUT_FILE))

    format_type = clean_text(get_format(config)).lower()
    topic = clean_text(get_topic(config))
    story_text = get_complete_input_story(config, INPUT_FILE)

    try:
        configured_parts = int(get_parts(config))
        scenes_per_part = int(get_scenes(config))
    except (TypeError, ValueError):
        raise RuntimeError("PARTS and SCENES must be valid integers.")

    if format_type not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {format_type!r}.")

    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")

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
        dynamic_short = False
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
        and old_plan.get("source_fingerprint") == plan.get("source_fingerprint")
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
    log("External API calls: 0")
    log("Output:", OUTPUT_FILE)


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