#!/usr/bin/env python3
"""Katha Lok AI — story-grounded planning with separate extra scenes."""

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

VERSION = "story-plan-v9-separate-hook-suspense-resolution"
MAPPING_VERSION = 5
MAX_PARTS = 1000
MAX_TOTAL_MAIN_SCENES = 1000

SHORT_BEATS = (
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
)

FULL_BEATS = (
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
)

SENTENCE_ENDINGS = re.compile(r"(?<=[।॥.!?！？…])\s+")


def log(*values: Any, file=None) -> None:
    print(*values, file=file or sys.stdout, flush=True)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def word_count(value: Any) -> int:
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


def as_bool(
    config: dict,
    key: str,
    default: bool = True,
) -> bool:
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
    """Split at paragraph and sentence boundaries, retaining punctuation."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    result = []

    for block in re.split(r"\n+", text):
        block = block.strip()
        if not block:
            continue

        pieces = SENTENCE_ENDINGS.split(block)

        for sentence in pieces:
            sentence = clean_text(sentence)
            if sentence:
                result.append(sentence)

    return result


def _split_long_sentence(
    sentence: str,
    maximum: int = 180,
) -> list[str]:
    """Split exceptionally long units while retaining token order."""
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
    Distribute all story sentences over main scenes chronologically.
    No source sentence is silently discarded or reordered.
    """
    if total_scenes < 1:
        raise RuntimeError("Total main scene count must be positive.")

    sentences = []

    for sentence in split_sentences(story_text):
        sentences.extend(_split_long_sentence(sentence))

    if not sentences:
        fallback = clean_text(topic)

        if not fallback:
            raise RuntimeError(
                "Cannot create story anchors: story text and topic are empty."
            )

        return [fallback] * total_scenes

    if len(sentences) <= total_scenes:
        return sentences + [""] * (total_scenes - len(sentences))

    weights = [
        max(1, word_count(sentence))
        for sentence in sentences
    ]

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


def select_beat(
    format_type: str,
    index: int,
    total: int,
) -> tuple[str, str]:
    beats = SHORT_BEATS if format_type == "short" else FULL_BEATS

    if total <= 1:
        return beats[0]

    position = round(
        index * (len(beats) - 1) / (total - 1)
    )
    position = max(0, min(position, len(beats) - 1))

    return beats[position]


def _part_anchor(
    anchors: list[str],
    start: int,
    end: int,
    topic: str,
) -> str:
    value = clean_text(" ".join(anchors[start:end]))
    return value or clean_text(topic)


def _make_main_scene(
    format_type: str,
    part_number: int,
    scene_number: int,
    global_number: int,
    total_main_scenes: int,
    anchor: str,
    topic: str,
) -> dict:
    beat, description = select_beat(
        format_type,
        global_number - 1,
        total_main_scenes,
    )

    subject = clean_text(anchor) or clean_text(topic)

    purpose = (
        f"{description} Relevant chronological story material: "
        f"{subject}. Preserve source chronology and causality."
        if subject else description
    )

    return {
        "part": part_number,
        "scene": scene_number,
        "global_scene_number": global_number,
        "scene_id": (
            f"part_{part_number:02d}_scene_{scene_number:02d}"
        ),
        "role": "main",
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
    }


def _make_extra_scene(
    part_number: int,
    kind: str,
    anchor: str,
    topic: str,
    format_type: str,
    final_part: bool,
) -> dict:
    """Create a separate Hook, Suspense or Resolution record."""
    anchor = clean_text(anchor) or clean_text(topic)

    if kind == "hook":
        role = "hook"
        scene_id = f"part_{part_number:02d}_hook"
        title = f"भाग {part_number} — Hook"
        purpose = (
            "Create an immediate, story-specific opening that raises "
            "curiosity, danger or an important question using only "
            "events and facts established in the story."
        )
        instruction = (
            "The hook is a separate opening scene. It must not replace, "
            "renumber, or alter any main story scene. Do not invent an "
            "event, character, clue or outcome."
        )

    elif kind == "suspense":
        role = "suspense"
        scene_id = f"part_{part_number:02d}_suspense"
        title = f"भाग {part_number} — Suspense"
        purpose = (
            "Create a genuine unresolved story-related question or "
            "consequence from the end of this part. The suspense must "
            "lead naturally into the next part."
        )
        instruction = (
            "This is a separate ending scene for a non-final part. "
            "Do not resolve the central story prematurely. Do not "
            "fabricate a cliffhanger or unsupported twist."
        )

    else:
        role = "resolution"
        scene_id = f"part_{part_number:02d}_resolution"
        title = f"भाग {part_number} — Resolution"
        purpose = (
            "Deliver the actual resolution of the complete story, "
            "showing the consequences of established events and "
            "closing the central conflict."
        )
        instruction = (
            "This is a separate final resolution scene. Use the actual "
            "source ending. Do not create a new ending or leave an "
            "artificial cliffhanger."
        )

    if not anchor:
        anchor = clean_text(topic)

    return {
        "part": part_number,
        "scene": None,
        "global_scene_number": None,
        "scene_id": scene_id,
        "role": role,
        "kind": kind,
        "title": title,
        "beat": role,
        "beat_description": purpose,
        "purpose": purpose,
        "story_anchor": anchor,
        "source_format": format_type,
        "is_final_part": bool(final_part),
        "separate_from_main_scenes": True,
        "main_scene_numbering_affected": False,
        "instruction": instruction,
        "continuity_rule": (
            "Maintain the same character identities, appearance, "
            "clothing, location, objects and chronology as the main "
            "story scenes in this part."
        ),
        "invention_policy": (
            "Use only established story facts. Do not add unrelated "
            "characters, objects, locations, events, dialogue or twists."
        ),
        "visual_policy": (
            "Depict the exact story-specific hook, suspense or resolution "
            "moment. Use live-action photorealism and avoid generic imagery."
        ),
    }


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
    Build exact main-scene counts plus separately numbered extra scenes.

    The main scene numbering remains part_XX_scene_YY.
    Hook/Suspense/Resolution records use separate IDs and never consume
    the configured main scene count.
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

    if not 1 <= parts_count <= MAX_PARTS:
        raise RuntimeError(
            f"PARTS must be between 1 and {MAX_PARTS}."
        )

    if scenes_per_part < 1:
        raise RuntimeError("SCENES must be a positive integer.")

    total_main_scenes = parts_count * scenes_per_part

    if total_main_scenes > MAX_TOTAL_MAIN_SCENES:
        raise RuntimeError(
            f"Requested plan exceeds {MAX_TOTAL_MAIN_SCENES} main scenes."
        )

    if not topic and not story_text:
        raise RuntimeError(
            "A topic or complete story is required for planning."
        )

    anchors = story_anchors(
        story_text,
        topic,
        total_main_scenes,
    )

    part_hook = as_bool(config, "PART_HOOK", True)
    part_suspense = as_bool(config, "PART_SUSPENSE", True)
    final_resolution = as_bool(config, "FINAL_RESOLUTION", True)

    parts = []
    global_number = 0

    for part_number in range(1, parts_count + 1):
        start = (part_number - 1) * scenes_per_part
        end = start + scenes_per_part
        part_anchors = anchors[start:end]

        scenes = []

        for scene_number, anchor in enumerate(part_anchors, 1):
            global_number += 1

            scenes.append(
                _make_main_scene(
                    format_type=format_type,
                    part_number=part_number,
                    scene_number=scene_number,
                    global_number=global_number,
                    total_main_scenes=total_main_scenes,
                    anchor=anchor,
                    topic=topic,
                )
            )

        part_story_anchor = _part_anchor(
            anchors,
            start,
            end,
            topic,
        )

        first_anchor = clean_text(
            part_anchors[0] if part_anchors else ""
        ) or part_story_anchor

        last_anchor = clean_text(
            part_anchors[-1] if part_anchors else ""
        ) or part_story_anchor

        is_final_part = part_number == parts_count

        hook_scene = (
            _make_extra_scene(
                part_number,
                "hook",
                first_anchor,
                topic,
                format_type,
                is_final_part,
            )
            if part_hook
            else None
        )

        suspense_scene = None
        resolution_scene = None

        if is_final_part:
            if final_resolution:
                resolution_scene = _make_extra_scene(
                    part_number,
                    "resolution",
                    last_anchor,
                    topic,
                    format_type,
                    True,
                )
        elif part_suspense:
            suspense_scene = _make_extra_scene(
                part_number,
                "suspense",
                last_anchor,
                topic,
                format_type,
                False,
            )

        parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": f"भाग {part_number}",
            "main_scene_count": scenes_per_part,
            "hook_required": bool(part_hook),
            "suspense_required": bool(
                part_suspense and not is_final_part
            ),
            "final_resolution": bool(
                final_resolution and is_final_part
            ),
            "story_anchor": part_story_anchor,
            "hook_scene": hook_scene,
            "suspense_scene": suspense_scene,
            "resolution_scene": resolution_scene,
            "extra_scenes": [
                item
                for item in (
                    hook_scene,
                    suspense_scene,
                    resolution_scene,
                )
                if isinstance(item, dict)
            ],
            "scenes": scenes,
        })

    source_payload = {
        "script_version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_main_scenes": total_main_scenes,
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
        "total_scenes": total_main_scenes,
        "total_main_scenes": total_main_scenes,
        "total_hook_scenes": sum(
            1 for part in parts if part["hook_scene"]
        ),
        "total_suspense_scenes": sum(
            1 for part in parts if part["suspense_scene"]
        ),
        "total_resolution_scenes": sum(
            1 for part in parts if part["resolution_scene"]
        ),
        "total_extra_scenes": sum(
            len(part["extra_scenes"]) for part in parts
        ),
        "scene_order": "hook_then_main_then_suspense_or_resolution",
        "main_scene_order": "part_then_scene",
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

def validate_extra_scene(
    record: Any,
    part_number: int,
    kind: str,
    required: bool,
    final_part: bool,
) -> None:
    if not required:
        if record is not None:
            raise RuntimeError(
                f"Part {part_number} has an unexpected {kind} scene."
            )
        return

    if not isinstance(record, dict):
        raise RuntimeError(
            f"Part {part_number} is missing its separate {kind} scene."
        )

    expected_id = f"part_{part_number:02d}_{kind}"

    if clean_text(record.get("scene_id")) != expected_id:
        raise RuntimeError(
            f"Incorrect {kind} scene ID in part {part_number}."
        )

    if clean_text(record.get("role")) != kind:
        raise RuntimeError(
            f"Incorrect {kind} role in part {part_number}."
        )

    if record.get("part") != part_number:
        raise RuntimeError(
            f"Incorrect part mapping in {expected_id}."
        )

    if record.get("scene") is not None:
        raise RuntimeError(
            f"{expected_id} must not use a main scene number."
        )

    if record.get("global_scene_number") is not None:
        raise RuntimeError(
            f"{expected_id} must not consume a main global scene number."
        )

    if record.get("separate_from_main_scenes") is not True:
        raise RuntimeError(
            f"{expected_id} is not marked as a separate scene."
        )

    if record.get("main_scene_numbering_affected") is not False:
        raise RuntimeError(
            f"{expected_id} must not affect main scene numbering."
        )

    for field in (
        "kind",
        "title",
        "beat",
        "beat_description",
        "purpose",
        "story_anchor",
        "instruction",
        "continuity_rule",
        "invention_policy",
        "visual_policy",
    ):
        if not clean_text(record.get(field)):
            raise RuntimeError(
                f"Missing {field} in {expected_id}."
            )

    if clean_text(record.get("kind")) != kind:
        raise RuntimeError(
            f"Incorrect extra-scene kind in {expected_id}."
        )

    if bool(record.get("is_final_part")) != bool(final_part):
        raise RuntimeError(
            f"Incorrect final-part flag in {expected_id}."
        )


def validate_plan(
    plan: dict,
    format_type: str,
    topic: str,
    parts_count: int,
    scenes_per_part: int,
) -> None:
    """Validate main scenes and separately planned extra scenes."""
    if not isinstance(plan, dict):
        raise RuntimeError("Story plan must be a JSON object.")

    format_type = clean_text(format_type).lower()
    topic = clean_text(topic)

    try:
        parts_count = int(parts_count)
        scenes_per_part = int(scenes_per_part)
    except (TypeError, ValueError):
        raise RuntimeError(
            "Expected part and scene counts must be integers."
        )

    if format_type not in {"short", "full"}:
        raise RuntimeError("Invalid plan format.")

    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError(
            "Plan part and scene counts must be positive."
        )

    expected_total = parts_count * scenes_per_part

    if expected_total > MAX_TOTAL_MAIN_SCENES:
        raise RuntimeError(
            "Plan exceeds the main-scene safety limit."
        )

    expected_values = {
        "status": "planned",
        "format": format_type,
        "topic": topic,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_total,
        "total_main_scenes": expected_total,
        "scene_order": "hook_then_main_then_suspense_or_resolution",
        "main_scene_order": "part_then_scene",
        "mapping_version": MAPPING_VERSION,
    }

    for key, expected in expected_values.items():
        if plan.get(key) != expected:
            raise RuntimeError(
                f"Story plan mismatch for {key}: "
                f"expected {expected!r}, got {plan.get(key)!r}."
            )

    if clean_text(plan.get("title")) != topic:
        raise RuntimeError(
            "Story plan title does not match the configured topic."
        )

    if not re.fullmatch(
        r"[a-fA-F0-9]{64}",
        clean_text(plan.get("source_fingerprint")),
    ):
        raise RuntimeError(
            "Story plan source_fingerprint is invalid."
        )

    source = plan.get("source")

    if not isinstance(source, dict):
        raise RuntimeError(
            "Story plan source metadata is missing."
        )

    if source.get("api_calls") != 0:
        raise RuntimeError(
            "Story planning must not make external API calls."
        )

    if source.get("planning_mode") != "offline_story_grounded":
        raise RuntimeError(
            "Unexpected story planning mode."
        )

    parts = plan.get("parts")

    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(
            f"Story plan must contain exactly {parts_count} parts."
        )

    total_hooks = 0
    total_suspense = 0
    total_resolutions = 0
    global_number = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(
                f"Invalid plan part {part_number}."
            )

        if part.get("part") != part_number:
            raise RuntimeError(
                f"Incorrect part numbering at part {part_number}."
            )

        expected_part_id = f"part_{part_number:02d}"

        if clean_text(part.get("part_id")) != expected_part_id:
            raise RuntimeError(
                f"Incorrect part ID: {expected_part_id}."
            )

        if part.get("main_scene_count") != scenes_per_part:
            raise RuntimeError(
                f"Main scene count metadata mismatch in part {part_number}."
            )

        scenes = part.get("scenes")

        if (
            not isinstance(scenes, list)
            or len(scenes) != scenes_per_part
        ):
            raise RuntimeError(
                f"Part {part_number} must contain exactly "
                f"{scenes_per_part} main scenes."
            )

        if not clean_text(part.get("story_anchor")):
            raise RuntimeError(
                f"Part {part_number} has no story anchor."
            )

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1

            expected_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Invalid main scene: {expected_id}."
                )

            required_values = {
                "part": part_number,
                "scene": scene_number,
                "global_scene_number": global_number,
                "scene_id": expected_id,
                "role": "main",
            }

            for field, expected in required_values.items():
                if scene.get(field) != expected:
                    raise RuntimeError(
                        f"Incorrect {field} in {expected_id}: "
                        f"expected {expected!r}, "
                        f"got {scene.get(field)!r}."
                    )

            for field in (
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

        is_final_part = part_number == parts_count

        hook_required = bool(part.get("hook_required"))
        suspense_required = bool(part.get("suspense_required"))
        resolution_required = bool(part.get("final_resolution"))

        if not hook_required:
            raise RuntimeError(
                f"Part {part_number} must have a separate hook scene."
            )

        if is_final_part:
            if suspense_required:
                raise RuntimeError(
                    "The final part cannot require a suspense cliffhanger."
                )

            if not resolution_required:
                raise RuntimeError(
                    "The final part must have a resolution scene."
                )
        else:
            if resolution_required:
                raise RuntimeError(
                    f"Non-final part {part_number} cannot contain final resolution."
                )

            if not suspense_required:
                raise RuntimeError(
                    f"Non-final part {part_number} must have suspense."
                )

        hook = part.get("hook_scene")
        suspense = part.get("suspense_scene")
        resolution = part.get("resolution_scene")

        validate_extra_scene(
            hook,
            part_number,
            "hook",
            hook_required,
            is_final_part,
        )

        validate_extra_scene(
            suspense,
            part_number,
            "suspense",
            suspense_required,
            False,
        )

        validate_extra_scene(
            resolution,
            part_number,
            "resolution",
            resolution_required,
            True,
        )

        expected_extra_scenes = [
            item
            for item in (hook, suspense, resolution)
            if isinstance(item, dict)
        ]

        extra_scenes = part.get("extra_scenes")

        if not isinstance(extra_scenes, list):
            raise RuntimeError(
                f"Part {part_number} extra_scenes must be a list."
            )

        if extra_scenes != expected_extra_scenes:
            raise RuntimeError(
                f"Part {part_number} extra_scenes list does not match "
                "its separate Hook/Suspense/Resolution records."
            )

        total_hooks += int(hook is not None)
        total_suspense += int(suspense is not None)
        total_resolutions += int(resolution is not None)

    if global_number != expected_total:
        raise RuntimeError(
            "Main scene global numbering is incorrect."
        )

    expected_hooks = parts_count
    expected_suspense = max(0, parts_count - 1)
    expected_resolutions = 1

    expected_totals = {
        "total_scenes": expected_total,
        "total_main_scenes": expected_total,
        "total_hook_scenes": expected_hooks,
        "total_suspense_scenes": expected_suspense,
        "total_resolution_scenes": expected_resolutions,
        "total_extra_scenes": (
            expected_hooks
            + expected_suspense
            + expected_resolutions
        ),
    }

    for field, expected in expected_totals.items():
        try:
            actual = int(plan.get(field, -1))
        except (TypeError, ValueError):
            actual = -1

        if actual != expected:
            raise RuntimeError(
                f"Plan metadata mismatch for {field}: "
                f"expected {expected}, got {actual}."
            )

    if total_hooks != expected_hooks:
        raise RuntimeError(
            "Separate Hook scene count does not match part count."
        )

    if total_suspense != expected_suspense:
        raise RuntimeError(
            "Separate Suspense scene count does not match non-final parts."
        )

    if total_resolutions != expected_resolutions:
        raise RuntimeError(
            "The plan must contain exactly one final Resolution scene."
        )


def read_story_text_direct(path: Path) -> str:
    """
    Read the complete multiline STORY_TEXT block independently of the
    configuration parser.
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
                trailing = remainder[
                    closing + len(delimiter):
                ].strip()

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

        if len(value) >= 2 and value[0] == value[-1]:
            if value[0] in {"'", '"'}:
                return value[1:-1].strip()

        return value

    return ""


def get_complete_input_story(
    config: dict,
    path: Path,
) -> str:
    """Use the complete direct input if the config parser truncated it."""
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
    story_text = get_complete_input_story(
        config,
        INPUT_FILE,
    )

    try:
        configured_parts = int(get_parts(config))
        scenes_per_part = int(get_scenes(config))
    except (TypeError, ValueError):
        raise RuntimeError(
            "PARTS and SCENES must be valid integers."
        )

    if format_type not in {"short", "full"}:
        raise RuntimeError(
            f"Unsupported FORMAT: {format_type!r}."
        )

    if configured_parts < 1 or scenes_per_part < 1:
        raise RuntimeError(
            "PARTS and SCENES must be positive integers."
        )

    if configured_parts > MAX_PARTS:
        raise RuntimeError(
            f"PARTS cannot exceed {MAX_PARTS}."
        )

    if configured_parts * scenes_per_part > MAX_TOTAL_MAIN_SCENES:
        raise RuntimeError(
            f"Requested plan exceeds {MAX_TOTAL_MAIN_SCENES} main scenes."
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

    parts_count = configured_parts

    plan = build_plan(
        config=config,
        format_type=format_type,
        topic=topic,
        story_text=story_text,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
        dynamic_short=False,
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

        except (RuntimeError, TypeError, ValueError, KeyError) as exc:
            log(
                "Existing plan is invalid; rebuilding:",
                exc,
            )

    write_json(OUTPUT_FILE, plan)

    main_anchor_count = sum(
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
    log("Main scenes per part:", scenes_per_part)
    log("Total main scenes:", plan["total_main_scenes"])
    log("Separate Hook scenes:", plan["total_hook_scenes"])
    log("Separate Suspense scenes:", plan["total_suspense_scenes"])
    log("Separate Resolution scenes:", plan["total_resolution_scenes"])
    log("Story-grounded main scenes:", main_anchor_count)
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
        log(
            type(exc).__name__ + ":",
            str(exc),
            file=sys.stderr,
        )
        sys.exit(1)