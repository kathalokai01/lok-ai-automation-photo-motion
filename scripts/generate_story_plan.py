#!/usr/bin/env python3
"""Build and validate the final story-grounded scene plan."""

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
    get_selected_parts,
)

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "story.json"
OUTPUT_FILE = ROOT / "output" / "story" / "story_plan.json"

VERSION = "story-plan-v11-final-story-parts-stable-role-mapping"
MAPPING_VERSION = 7

MAX_PARTS = 1000
MAX_TOTAL_MAIN_SCENES = 1000

SENTENCE_ENDINGS = re.compile(r"(?<=[।॥.!?！？…])\s+")
CLAUSE_ENDINGS = re.compile(r"(?<=[,，;:])\s+")

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


def log(*values: Any, file=None) -> None:
    print(*values, file=file or sys.stdout, flush=True)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_text(value: Any) -> str:
    text = str(value or "").replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
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


def split_sentences(text: str) -> list[str]:
    """Split at complete sentence and paragraph boundaries."""
    normalized = normalize_text(text)
    result = []

    for block in re.split(r"\n+", normalized):
        if not block.strip():
            continue

        for sentence in SENTENCE_ENDINGS.split(block):
            sentence = clean_text(sentence)
            if sentence:
                result.append(sentence)

    return result


def split_clauses(sentence: str) -> list[str]:
    """Use punctuation-based clause boundaries only when needed."""
    sentence = clean_text(sentence)
    if not sentence:
        return []

    pieces = [
        clean_text(piece)
        for piece in CLAUSE_ENDINGS.split(sentence)
        if clean_text(piece)
    ]
    return pieces or [sentence]


def split_natural_units(text: str, required_count: int) -> list[str]:
    """
    Prefer complete sentences. Use comma/semicolon/colon boundaries only
    when the complete story does not contain enough sentence boundaries.
    Never split by arbitrary word position.
    """
    sentences = split_sentences(text)

    if not sentences:
        raise RuntimeError("Complete story contains no usable sentences.")

    units = list(sentences)

    if len(units) < required_count:
        expanded = []
        for sentence in sentences:
            expanded.extend(split_clauses(sentence))
        units = expanded

    if len(units) < required_count:
        raise RuntimeError(
            f"Only {len(units)} natural sentence/clause boundaries exist "
            f"for {required_count} requested scenes. Mid-sentence splitting "
            "is not permitted."
        )

    return units


def group_natural_units(units: list[str], count: int) -> list[str]:
    """Distribute chronological sentence/clause units without loss."""
    if count < 1 or len(units) < count:
        raise RuntimeError("Insufficient natural units for scene mapping.")

    groups = []
    cursor = 0
    remaining_words = sum(word_count(unit) for unit in units)

    for group_index in range(count):
        remaining_groups = count - group_index
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
            next_unit = units[cursor]
            next_words = word_count(next_unit)

            if selected and abs(selected_words - target) <= abs(
                selected_words + next_words - target
            ):
                break

            selected.append(next_unit)
            selected_words += next_words
            cursor += 1

            if selected_words >= target:
                break

        if not selected:
            raise RuntimeError("Unable to map a non-empty scene anchor.")

        groups.append(" ".join(selected))
        remaining_words -= selected_words

    if cursor != len(units) or len(groups) != count:
        raise RuntimeError("Natural story grouping did not finish correctly.")

    original = clean_text(" ".join(units))
    reconstructed = clean_text(" ".join(groups))

    if original != reconstructed:
        raise RuntimeError(
            "Story mapping lost, duplicated or reordered story material."
        )

    if any(not clean_text(group) for group in groups):
        raise RuntimeError("One or more scene anchors are empty.")

    return groups


def story_anchors(
    story_text: str,
    topic: str,
    total_scenes: int,
) -> list[str]:
    """Map the complete corrected story to exact chronological scene anchors."""
    if total_scenes < 1:
        raise RuntimeError("Total main scene count must be positive.")

    text = normalize_text(story_text)

    if not text:
        raise RuntimeError(
            "A complete corrected story is required for scene mapping."
        )

    units = split_natural_units(text, total_scenes)
    anchors = group_natural_units(units, total_scenes)

    if len(anchors) != total_scenes:
        raise RuntimeError("Scene anchor count does not match the final plan.")

    if word_count(" ".join(anchors)) != word_count(text):
        raise RuntimeError("Scene anchors do not preserve the complete story.")

    return anchors


def select_beat(
    format_type: str,
    index: int,
    total: int,
) -> tuple[str, str]:
    beats = SHORT_BEATS if format_type == "short" else FULL_BEATS

    if total <= 1:
        return beats[0]

    position = round(index * (len(beats) - 1) / (total - 1))
    position = max(0, min(position, len(beats) - 1))
    return beats[position]


def canonical_main_id(part_number: int, scene_number: int) -> str:
    return f"part_{part_number:02d}_scene_{scene_number:02d}"


def canonical_extra_id(part_number: int, role: str) -> str:
    return f"part_{part_number:02d}_{role}"


def _make_main_scene(
    format_type: str,
    part_number: int,
    scene_number: int,
    global_number: int,
    total_main_scenes: int,
    anchor: str,
) -> dict:
    beat, description = select_beat(
        format_type,
        global_number - 1,
        total_main_scenes,
    )
    anchor = clean_text(anchor)

    if not anchor:
        raise RuntimeError(
            f"Main scene {global_number} has no story anchor."
        )

    return {
        "part": part_number,
        "scene": scene_number,
        "global_scene_number": global_number,
        "scene_id": canonical_main_id(part_number, scene_number),
        "role": "main",
        "beat": beat,
        "beat_description": description,
        "purpose": (
            f"{description} Story material: {anchor} "
            "Preserve chronology, causality and source meaning."
        ),
        "story_anchor": anchor,
        "continuity_rule": (
            "Maintain established character identity, age, face, body, "
            "clothing, location, objects, chronology and physical continuity."
        ),
        "invention_policy": (
            "Do not invent unsupported characters, relationships, motives, "
            "locations, objects, events, dialogue, clues or twists."
        ),
        "visual_policy": (
            "Depict the exact narrated story moment. Do not use unrelated "
            "stock scenes, generic imagery or events from another part."
        ),
    }


def _make_extra_scene(
    part_number: int,
    role: str,
    anchor: str,
    format_type: str,
    final_part: bool,
) -> dict:
    """Create a separate role record without consuming main scene numbering."""
    anchor = clean_text(anchor)

    if not anchor:
        raise RuntimeError(
            f"Part {part_number} {role} record has no story anchor."
        )

    definitions = {
        "hook": {
            "purpose": (
                "Create a story-specific opening that raises curiosity, "
                "danger or an important question using established facts."
            ),
            "instruction": (
                "Separate opening scene. Do not replace or renumber main "
                "scenes. Do not invent an event, character, clue or outcome."
            ),
        },
        "suspense": {
            "purpose": (
                "Create an unresolved question or consequence from this "
                "non-final part's ending that leads naturally into the next."
            ),
            "instruction": (
                "Separate ending scene for a non-final part. Do not resolve "
                "the central story prematurely or fabricate a cliffhanger."
            ),
        },
        "resolution": {
            "purpose": (
                "Deliver the actual complete-story resolution and close "
                "the central conflict using the established ending."
            ),
            "instruction": (
                "Separate final resolution scene. Do not invent a new ending "
                "or leave an artificial cliffhanger."
            ),
        },
    }

    if role not in definitions:
        raise RuntimeError(f"Unsupported extra-scene role: {role}")

    definition = definitions[role]

    return {
        "part": part_number,
        "scene": None,
        "global_scene_number": None,
        "scene_id": canonical_extra_id(part_number, role),
        "role": role,
        "kind": role,
        "title": f"भाग {part_number} — {role.title()}",
        "beat": role,
        "beat_description": definition["purpose"],
        "purpose": definition["purpose"],
        "story_anchor": anchor,
        "source_format": format_type,
        "is_final_part": bool(final_part),
        "separate_from_main_scenes": True,
        "main_scene_numbering_affected": False,
        "instruction": definition["instruction"],
        "continuity_rule": (
            "Maintain the same established character identities, appearance, "
            "clothing, locations, objects and chronology."
        ),
        "invention_policy": (
            "Use only established story facts. Do not add unrelated "
            "characters, objects, locations, events, dialogue or twists."
        ),
        "visual_policy": (
            "Depict the exact story-specific role moment with cinematic "
            "live-action realism; avoid generic imagery."
        ),
    }


def _build_part_records(
    format_type: str,
    anchors: list[str],
    parts_count: int,
    scenes_per_part: int,
) -> list[dict]:
    parts = []
    global_number = 0

    for part_number in range(1, parts_count + 1):
        start = (part_number - 1) * scenes_per_part
        end = start + scenes_per_part
        part_anchors = anchors[start:end]

        if len(part_anchors) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number} has an incorrect number of scene anchors."
            )

        scenes = []

        for scene_number, anchor in enumerate(part_anchors, 1):
            global_number += 1
            scenes.append(
                _make_main_scene(
                    format_type,
                    part_number,
                    scene_number,
                    global_number,
                    parts_count * scenes_per_part,
                    anchor,
                )
            )

        first_anchor = part_anchors[0]
        last_anchor = part_anchors[-1]
        is_final = part_number == parts_count

        hook = _make_extra_scene(
            part_number, "hook", first_anchor, format_type, is_final
        )

        suspense = (
            None if is_final else
            _make_extra_scene(
                part_number, "suspense", last_anchor, format_type, False
            )
        )

        resolution = (
            _make_extra_scene(
                part_number, "resolution", last_anchor, format_type, True
            ) if is_final else None
        )

        extras = [
            record
            for record in (hook, suspense, resolution)
            if isinstance(record, dict)
        ]

        parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": f"भाग {part_number}",
            "main_scene_count": scenes_per_part,
            "hook_required": True,
            "suspense_required": not is_final,
            "final_resolution": is_final,
            "story_anchor": clean_text(" ".join(part_anchors)),
            "hook_scene": hook,
            "suspense_scene": suspense,
            "resolution_scene": resolution,
            "extra_scenes": extras,
            "scenes": scenes,
        })

    return parts


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
    Public API used by generate_story.py.

    Always maps the complete final corrected story using the final selected
    part count. It never uses a stale topic-only plan or an old part count.
    """
    format_type = clean_text(format_type).lower()
    topic = clean_text(topic)
    story_text = normalize_text(story_text)

    if format_type not in {"short", "full"}:
        raise RuntimeError("FORMAT must be exactly short or full.")

    try:
        parts_count = int(parts_count)
        scenes_per_part = int(scenes_per_part)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("PARTS and SCENES must be integers.") from exc

    if not 1 <= parts_count <= MAX_PARTS:
        raise RuntimeError(f"PARTS must be between 1 and {MAX_PARTS}.")

    if scenes_per_part < 1:
        raise RuntimeError("SCENES must be a positive integer.")

    total_main_scenes = parts_count * scenes_per_part

    if total_main_scenes > MAX_TOTAL_MAIN_SCENES:
        raise RuntimeError(
            f"Plan exceeds {MAX_TOTAL_MAIN_SCENES} main scenes."
        )

    if not story_text:
        raise RuntimeError(
            "A complete corrected story is required before scene planning."
        )

    anchors = story_anchors(story_text, topic, total_main_scenes)
    parts = _build_part_records(
        format_type,
        anchors,
        parts_count,
        scenes_per_part,
    )

    source_payload = {
        "script_version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "format": format_type,
        "topic": topic,
        "story_text_sha256": hashlib.sha256(
            story_text.encode("utf-8")
        ).hexdigest(),
        "story_word_count": word_count(story_text),
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_main_scenes": total_main_scenes,
        "dynamic_short_parts": bool(dynamic_short),
        "parts": parts,
    }

    source_fingerprint = fingerprint(source_payload)

    plan = {
        "status": "planned",
        "script_version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "format": format_type,
        "topic": topic,
        "title": topic or "",
        "title_source": "configured_topic_or_final_story_title",
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total_main_scenes,
        "total_main_scenes": total_main_scenes,
        "total_hook_scenes": parts_count,
        "total_suspense_scenes": max(0, parts_count - 1),
        "total_resolution_scenes": 1,
        "total_extra_scenes": parts_count + max(0, parts_count - 1) + 1,
        "scene_order": "hook_then_main_then_suspense_or_resolution",
        "main_scene_order": "part_then_scene",
        "role_order": ["hook", "main", "suspense", "resolution"],
        "mapping_version": MAPPING_VERSION,
        "dynamic_short_parts": bool(dynamic_short),
        "source_fingerprint": source_fingerprint,
        "source_payload": source_payload,
        "source": {
            "input_file": "Input/topic.txt",
            "story_text_provided": True,
            "story_word_count": word_count(story_text),
            "story_text_sha256": source_payload["story_text_sha256"],
            "planning_mode": "final_corrected_story",
            "api_calls": 0,
        },
        "parts": parts,
    }

    validate_plan(
        plan,
        format_type,
        topic,
        parts_count,
        scenes_per_part,
    )

    return plan


def validate_extra_scene(
    record: Any,
    part_number: int,
    role: str,
    required: bool,
    final_part: bool,
) -> None:
    if not required:
        if record is not None:
            raise RuntimeError(
                f"Part {part_number} has an unexpected {role} record."
            )
        return

    if not isinstance(record, dict):
        raise RuntimeError(
            f"Part {part_number} is missing its separate {role} record."
        )

    expected_values = {
        "scene_id": canonical_extra_id(part_number, role),
        "role": role,
        "kind": role,
        "part": part_number,
        "scene": None,
        "global_scene_number": None,
        "separate_from_main_scenes": True,
        "main_scene_numbering_affected": False,
        "is_final_part": bool(final_part),
    }

    for field, expected in expected_values.items():
        if record.get(field) != expected:
            raise RuntimeError(
                f"Invalid {field} in part {part_number} {role} record."
            )

    for field in (
        "title",
        "beat",
        "beat_description",
        "purpose",
        "story_anchor",
        "source_format",
        "instruction",
        "continuity_rule",
        "invention_policy",
        "visual_policy",
    ):
        if not clean_text(record.get(field)):
            raise RuntimeError(
                f"Missing {field} in part {part_number} {role} record."
            )


def validate_plan(
    plan: dict,
    format_type: str,
    topic: str,
    parts_count: int,
    scenes_per_part: int,
) -> None:
    """Reject stale plans, incorrect IDs, role counts and unrelated mapping."""
    if not isinstance(plan, dict):
        raise RuntimeError("Story plan must be a JSON object.")

    format_type = clean_text(format_type).lower()
    topic = clean_text(topic)

    try:
        parts_count = int(parts_count)
        scenes_per_part = int(scenes_per_part)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Expected PARTS and SCENES must be integers.") from exc

    if format_type not in {"short", "full"}:
        raise RuntimeError("Invalid story plan format.")

    if not 1 <= parts_count <= MAX_PARTS or scenes_per_part < 1:
        raise RuntimeError("Invalid expected part or scene count.")

    expected_total = parts_count * scenes_per_part

    if expected_total > MAX_TOTAL_MAIN_SCENES:
        raise RuntimeError("Plan exceeds the main-scene safety limit.")

    expected_values = {
        "status": "planned",
        "script_version": VERSION,
        "format": format_type,
        "topic": topic,
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": expected_total,
        "total_main_scenes": expected_total,
        "scene_order": "hook_then_main_then_suspense_or_resolution",
        "main_scene_order": "part_then_scene",
        "role_order": ["hook", "main", "suspense", "resolution"],
        "mapping_version": MAPPING_VERSION,
    }

    for field, expected in expected_values.items():
        if plan.get(field) != expected:
            raise RuntimeError(
                f"Story plan mismatch for {field}: "
                f"expected {expected!r}, got {plan.get(field)!r}."
            )

    if clean_text(plan.get("title")) != topic:
        raise RuntimeError("Story plan topic/title metadata mismatch.")

    source = plan.get("source")
    payload = plan.get("source_payload")

    if not isinstance(source, dict) or not isinstance(payload, dict):
        raise RuntimeError("Story plan source payload is missing.")

    if source.get("api_calls") != 0:
        raise RuntimeError("Story planning must not make external API calls.")

    if source.get("planning_mode") != "final_corrected_story":
        raise RuntimeError("Plan was not generated from the final corrected story.")

    if source.get("story_text_provided") is not True:
        raise RuntimeError("Complete story source is missing.")

    if payload.get("format") != format_type:
        raise RuntimeError("Source payload format mismatch.")

    if payload.get("topic") != topic:
        raise RuntimeError("Source payload topic mismatch.")

    if payload.get("parts_count") != parts_count:
        raise RuntimeError("Source payload selected part count is stale.")

    if payload.get("scenes_per_part") != scenes_per_part:
        raise RuntimeError("Source payload scene count is stale.")

    if payload.get("total_main_scenes") != expected_total:
        raise RuntimeError("Source payload total main scene count mismatch.")

    expected_fingerprint = fingerprint(payload)

    if plan.get("source_fingerprint") != expected_fingerprint:
        raise RuntimeError("Story plan source fingerprint is invalid.")

    story_hash = clean_text(payload.get("story_text_sha256"))

    if not re.fullmatch(r"[a-f0-9]{64}", story_hash):
        raise RuntimeError("Final story SHA-256 is missing or invalid.")

    if source.get("story_text_sha256") != story_hash:
        raise RuntimeError("Source story hash metadata mismatch.")

    if source.get("story_word_count") != payload.get("story_word_count"):
        raise RuntimeError("Source story word-count metadata mismatch.")

    parts = plan.get("parts")

    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(
            f"Story plan must contain exactly {parts_count} parts."
        )

    total_hooks = 0
    total_suspense = 0
    total_resolutions = 0
    global_number = 0
    all_anchors = []

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid plan part {part_number}.")

        expected_part_values = {
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "main_scene_count": scenes_per_part,
            "hook_required": True,
            "suspense_required": part_number != parts_count,
            "final_resolution": part_number == parts_count,
        }

        for field, expected in expected_part_values.items():
            if part.get(field) != expected:
                raise RuntimeError(
                    f"Part {part_number} has invalid {field}."
                )

        scenes = part.get("scenes")

        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number} must contain exactly "
                f"{scenes_per_part} main scenes."
            )

        part_anchors = []

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            expected_id = canonical_main_id(
                part_number, scene_number
            )

            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid main scene {expected_id}.")

            expected_scene_values = {
                "part": part_number,
                "scene": scene_number,
                "global_scene_number": global_number,
                "scene_id": expected_id,
                "role": "main",
            }

            for field, expected in expected_scene_values.items():
                if scene.get(field) != expected:
                    raise RuntimeError(
                        f"Invalid {field} in main scene {expected_id}."
                    )

            for field in (
                "beat",
                "beat_description",
                "purpose",
                "story_anchor",
                "continuity_rule",
                "invention_policy",
                "visual_policy",
            ):
                if not clean_text(scene.get(field)):
                    raise RuntimeError(
                        f"Missing {field} in main scene {expected_id}."
                    )

            anchor = clean_text(scene.get("story_anchor"))
            part_anchors.append(anchor)
            all_anchors.append(anchor)

        if clean_text(" ".join(part_anchors)) != clean_text(
            part.get("story_anchor")
        ):
            raise RuntimeError(
                f"Part {part_number} anchor differs from its scene anchors."
            )

        is_final = part_number == parts_count
        hook = part.get("hook_scene")
        suspense = part.get("suspense_scene")
        resolution = part.get("resolution_scene")

        validate_extra_scene(
            hook, part_number, "hook", True, is_final
        )
        validate_extra_scene(
            suspense, part_number, "suspense", not is_final, False
        )
        validate_extra_scene(
            resolution, part_number, "resolution", is_final, True
        )

        expected_extras = [
            record
            for record in (hook, suspense, resolution)
            if isinstance(record, dict)
        ]

        if part.get("extra_scenes") != expected_extras:
            raise RuntimeError(
                f"Part {part_number} extra role order or records are invalid."
            )

        if clean_text(hook.get("story_anchor")) != part_anchors[0]:
            raise RuntimeError(
                f"Part {part_number} Hook is not anchored to its opening."
            )

        if suspense is not None and clean_text(
            suspense.get("story_anchor")
        ) != part_anchors[-1]:
            raise RuntimeError(
                f"Part {part_number} Suspense is not anchored to its ending."
            )

        if resolution is not None and clean_text(
            resolution.get("story_anchor")
        ) != part_anchors[-1]:
            raise RuntimeError(
                "Final Resolution is not anchored to the final story scene."
            )

        total_hooks += 1
        total_suspense += int(suspense is not None)
        total_resolutions += int(resolution is not None)

    if global_number != expected_total:
        raise RuntimeError("Global main scene numbering is incorrect.")

    if len(all_anchors) != expected_total or any(
        not anchor for anchor in all_anchors
    ):
        raise RuntimeError("One or more main scene anchors are missing.")

    if sum(word_count(anchor) for anchor in all_anchors) != int(
        source.get("story_word_count", -1)
    ):
        raise RuntimeError(
            "Main scene anchors do not preserve the complete story word count."
        )

    expected_counts = {
        "total_scenes": expected_total,
        "total_main_scenes": expected_total,
        "total_hook_scenes": parts_count,
        "total_suspense_scenes": max(0, parts_count - 1),
        "total_resolution_scenes": 1,
        "total_extra_scenes": parts_count + max(0, parts_count - 1) + 1,
    }

    for field, expected in expected_counts.items():
        if plan.get(field) != expected:
            raise RuntimeError(
                f"Plan metadata mismatch for {field}."
            )

    if (
        total_hooks,
        total_suspense,
        total_resolutions,
    ) != (
        parts_count,
        max(0, parts_count - 1),
        1,
    ):
        raise RuntimeError("Hook/Suspense/Resolution role counts are invalid.")


def read_story_from_final_output() -> dict | None:
    data = read_json(STORY_FILE)

    if not isinstance(data, dict):
        return None

    if data.get("status") != "completed":
        return None

    return data


def get_final_story_data(config: dict) -> tuple[str, int, int, str]:
    """Use final corrected story and persisted actual parts when available."""
    final_story = read_story_from_final_output()

    if final_story:
        format_type = clean_text(final_story.get("format")).lower()
        if format_type != get_format(config):
            raise RuntimeError(
                "Final story format does not match Input/topic.txt."
            )

        story_text = normalize_text(
            final_story.get("corrected_complete_story")
            or final_story.get("story_text")
        )

        if not story_text:
            raise RuntimeError("Final story output has no complete story text.")

        try:
            parts_count = int(
                final_story.get("selected_parts_count")
                or final_story.get("parts_count")
                or final_story.get("total_parts")
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Final story does not contain its actual selected part count."
            ) from exc

        if parts_count < 1:
            raise RuntimeError("Final story selected part count is invalid.")

        final_parts = final_story.get("parts")
        if not isinstance(final_parts, list) or len(final_parts) != parts_count:
            raise RuntimeError(
                "Final story parts do not match its selected part count."
            )

        return (
            story_text,
            parts_count,
            get_scenes(config),
            clean_text(final_story.get("title") or get_topic(config)),
        )

    # Standalone use is allowed only when the selected count has already
    # been persisted. A range minimum must never be substituted.
    selected = get_selected_parts(config, required=True)

    if selected is None:
        raise RuntimeError(
            "Actual PARTS count has not been persisted by story generation."
        )

    story_text = normalize_text(get_story_text(config))

    if not story_text:
        raise RuntimeError(
            "No final story.json exists and Input/topic.txt has no STORY_TEXT."
        )

    return (
        story_text,
        int(selected),
        get_scenes(config),
        clean_text(get_topic(config)),
    )


def main() -> None:
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Input file is missing: {INPUT_FILE}")

    config = load_and_validate(str(INPUT_FILE))
    format_type = clean_text(get_format(config)).lower()
    topic = clean_text(get_topic(config))

    story_text, parts_count, scenes_per_part, final_title = (
        get_final_story_data(config)
    )

    if format_type not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {format_type!r}.")

    if not story_text:
        raise RuntimeError("Complete final story is empty.")

    if word_count(story_text) < 20:
        raise RuntimeError("Complete final story contains fewer than 20 words.")

    plan = build_plan(
        config=config,
        format_type=format_type,
        topic=topic,
        story_text=story_text,
        parts_count=parts_count,
        scenes_per_part=scenes_per_part,
        dynamic_short=False,
    )

    # Keep the actual final story title in the plan as metadata while
    # preserving the topic field required by existing callers.
    plan["title"] = final_title or topic
    plan["title_source"] = "final_corrected_story"
    plan["source_payload"]["final_title"] = final_title or topic
    plan["source_fingerprint"] = fingerprint(plan["source_payload"])

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
            log("Matching final-story plan checkpoint preserved.")
            log("External API calls: 0")
            return
        except (RuntimeError, TypeError, ValueError, KeyError) as exc:
            log("Existing plan invalid; rebuilding:", exc)

    write_json(OUTPUT_FILE, plan)

    log("STORY PLAN: SUCCESS")
    log("Version:", VERSION)
    log("Format:", format_type)
    log("Title:", final_title or topic or "(not supplied)")
    log("Final story words:", word_count(story_text))
    log("Actual selected parts:", parts_count)
    log("Main scenes per part:", scenes_per_part)
    log("Total main scenes:", plan["total_main_scenes"])
    log("Separate Hook scenes:", plan["total_hook_scenes"])
    log("Separate Suspense scenes:", plan["total_suspense_scenes"])
    log("Separate Resolution scenes:", plan["total_resolution_scenes"])
    log("Role order:", "hook → main → suspense/resolution")
    log("External API calls: 0")
    log("Output:", OUTPUT_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("STORY PLAN: CANCELLED")
        raise SystemExit(130)
    except Exception as exc:
        log("STORY PLAN: FAILED")
        log(type(exc).__name__ + ":", str(exc), file=sys.stderr)
        raise SystemExit(1)