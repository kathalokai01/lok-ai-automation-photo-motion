#!/usr/bin/env python3
"""Story-grounded scene planning with independent Hook, Suspense and Resolution."""

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

VERSION = "story-plan-v10-role-safe-complete-story-mapping"
MAPPING_VERSION = 6
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
CLAUSE_ENDINGS = re.compile(r"(?<=[,，;:])\s+")


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
    """Split only at complete sentence or paragraph boundaries."""
    text = normalize_text(text)
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


def split_clauses(sentence: str) -> list[str]:
    """Use punctuation-based clause boundaries only when necessary."""
    sentence = clean_text(sentence)
    if not sentence:
        return []
    pieces = [
        clean_text(piece)
        for piece in CLAUSE_ENDINGS.split(sentence)
        if clean_text(piece)
    ]
    return pieces or [sentence]


def _split_story_units(text: str, required_units: int) -> list[str]:
    """
    Create enough natural units for the requested scene count.
    Sentence boundaries are preferred; comma/semicolon/colon boundaries
    are secondary. Never split by arbitrary word count.
    """
    sentences = split_sentences(text)
    if not sentences:
        raise RuntimeError("Story text has no usable sentences.")

    units = list(sentences)
    if len(units) < required_units:
        expanded = []
        for sentence in units:
            expanded.extend(split_clauses(sentence))
        units = expanded

    if len(units) < required_units:
        raise RuntimeError(
            "The story does not contain enough natural sentence/clause "
            f"boundaries for {required_units} main scenes. "
            "The planner will not cut narration mid-sentence."
        )

    return units


def _group_units(units: list[str], count: int) -> list[str]:
    """Distribute natural units chronologically with balanced word counts."""
    if count < 1 or len(units) < count:
        raise RuntimeError("Insufficient natural story units for requested grouping.")

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
        max_take = remaining_units - (remaining_groups - 1)
        selected = []
        selected_words = 0

        while cursor < len(units) and len(selected) < max_take:
            next_words = word_count(units[cursor])
            if selected and abs(selected_words - target) <= abs(
                selected_words + next_words - target
            ):
                break

            selected.append(units[cursor])
            selected_words += next_words
            cursor += 1

            if selected_words >= target:
                break

        if not selected:
            selected.append(units[cursor])
            selected_words = word_count(units[cursor])
            cursor += 1

        groups.append(" ".join(selected))
        remaining_words -= selected_words

    if cursor != len(units) or len(groups) != count:
        raise RuntimeError("Natural story-unit grouping was incomplete.")

    if any(not clean_text(group) for group in groups):
        raise RuntimeError("Story mapping produced an empty main scene.")

    original = clean_text(" ".join(units))
    reconstructed = clean_text(" ".join(groups))
    if original != reconstructed:
        raise RuntimeError("Story mapping lost, duplicated or reordered text.")

    return groups


def story_anchors(
    story_text: str,
    topic: str,
    total_scenes: int,
) -> list[str]:
    """
    Map the complete story to main scenes in original chronological order.
    Every scene gets real story material; empty padded anchors are forbidden.
    """
    if total_scenes < 1:
        raise RuntimeError("Total main scene count must be positive.")

    text = normalize_text(story_text)
    if not text:
        if not clean_text(topic):
            raise RuntimeError("Story text and topic are both empty.")
        raise RuntimeError(
            "A topic alone cannot produce story-grounded scene anchors. "
            "Generate the complete story before planning scenes."
        )

    units = _split_story_units(text, total_scenes)
    anchors = _group_units(units, total_scenes)

    if len(anchors) != total_scenes:
        raise RuntimeError("Story anchor count does not match requested scenes.")

    for index, anchor in enumerate(anchors, 1):
        if not clean_text(anchor):
            raise RuntimeError(f"Main scene {index} has an empty story anchor.")

    if word_count(" ".join(anchors)) != word_count(text):
        raise RuntimeError("Story anchor mapping changed the total word count.")

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
            f"Cannot create main scene {global_number} without story text."
        )

    return {
        "part": part_number,
        "scene": scene_number,
        "global_scene_number": global_number,
        "scene_id": f"part_{part_number:02d}_scene_{scene_number:02d}",
        "role": "main",
        "beat": beat,
        "beat_description": description,
        "purpose": (
            f"{description} Story material: {anchor}. "
            "Preserve chronology, causality and the source meaning."
        ),
        "story_anchor": anchor,
        "continuity_rule": (
            "Keep character identity, age, face, body, clothing, location, "
            "objects, chronology and physical continuity consistent with "
            "the complete story and character bible."
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
    kind: str,
    anchor: str,
    format_type: str,
    final_part: bool,
) -> dict:
    """Create a separate Hook, Suspense or Resolution record."""
    anchor = clean_text(anchor)
    if not anchor:
        raise RuntimeError(
            f"Part {part_number} {kind} scene has no story anchor."
        )

    definitions = {
        "hook": {
            "title": f"भाग {part_number} — Hook",
            "purpose": (
                "Create an immediate story-specific opening that raises "
                "curiosity, danger or an important question using established "
                "story facts only."
            ),
            "instruction": (
                "This is a separate opening scene. It must not replace, "
                "renumber or alter any main story scene. Do not invent an "
                "event, character, clue, twist or outcome."
            ),
        },
        "suspense": {
            "title": f"भाग {part_number} — Suspense",
            "purpose": (
                "Create a genuine unresolved question or consequence from "
                "the end of this non-final part, leading naturally into "
                "the next part."
            ),
            "instruction": (
                "This is a separate ending scene for a non-final part. "
                "Do not resolve the central story prematurely. Do not "
                "fabricate a cliffhanger or unsupported twist."
            ),
        },
        "resolution": {
            "title": f"भाग {part_number} — Resolution",
            "purpose": (
                "Deliver the actual resolution of the complete story, "
                "showing the consequences of established events and "
                "closing the central conflict."
            ),
            "instruction": (
                "This is the separate final resolution scene. Use the "
                "actual source ending. Do not create a new ending or "
                "leave an artificial cliffhanger."
            ),
        },
    }

    if kind not in definitions:
        raise RuntimeError(f"Unsupported extra-scene kind: {kind}")

    definition = definitions[kind]
    return {
        "part": part_number,
        "scene": None,
        "global_scene_number": None,
        "scene_id": f"part_{part_number:02d}_{kind}",
        "role": kind,
        "kind": kind,
        "title": definition["title"],
        "beat": kind,
        "beat_description": definition["purpose"],
        "purpose": definition["purpose"],
        "story_anchor": anchor,
        "source_format": format_type,
        "is_final_part": bool(final_part),
        "separate_from_main_scenes": True,
        "main_scene_numbering_affected": False,
        "instruction": definition["instruction"],
        "continuity_rule": (
            "Maintain the same character identities, appearance, clothing, "
            "locations, objects and chronology as the main story scenes."
        ),
        "invention_policy": (
            "Use only established story facts. Do not add unrelated "
            "characters, objects, locations, events, dialogue or twists."
        ),
        "visual_policy": (
            "Depict the exact story-specific hook, suspense or resolution "
            "moment. Use cinematic live-action realism and avoid generic imagery."
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
    Build exact main-scene counts and independent extra-scene records.

    Every part gets a Hook.
    Every non-final part gets Suspense.
    The final part gets Resolution and no Suspense.
    These records never consume main scene numbering.
    """
    format_type = clean_text(format_type).lower()
    topic = clean_text(topic)
    story_text = normalize_text(story_text)

    if format_type not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")

    try:
        parts_count = int(parts_count)
        scenes_per_part = int(scenes_per_part)
    except (TypeError, ValueError):
        raise RuntimeError("PARTS and SCENES must be integers.")

    if not 1 <= parts_count <= MAX_PARTS:
        raise RuntimeError(f"PARTS must be between 1 and {MAX_PARTS}.")
    if scenes_per_part < 1:
        raise RuntimeError("SCENES must be a positive integer.")

    total_main_scenes = parts_count * scenes_per_part
    if total_main_scenes > MAX_TOTAL_MAIN_SCENES:
        raise RuntimeError(
            f"Requested plan exceeds {MAX_TOTAL_MAIN_SCENES} main scenes."
        )
    if not story_text:
        raise RuntimeError(
            "A complete story is required before story-grounded planning."
        )

    anchors = story_anchors(story_text, topic, total_main_scenes)
    parts = []
    global_number = 0

    for part_number in range(1, parts_count + 1):
        start = (part_number - 1) * scenes_per_part
        end = start + scenes_per_part
        part_anchors = anchors[start:end]
        if len(part_anchors) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number} does not have the required scene anchors."
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
                    total_main_scenes,
                    anchor,
                )
            )

        part_anchor = clean_text(" ".join(part_anchors))
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
            item for item in (hook, suspense, resolution)
            if isinstance(item, dict)
        ]

        parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": f"भाग {part_number}",
            "main_scene_count": scenes_per_part,
            "hook_required": True,
            "suspense_required": not is_final,
            "final_resolution": is_final,
            "story_anchor": part_anchor,
            "hook_scene": hook,
            "suspense_scene": suspense,
            "resolution_scene": resolution,
            "extra_scenes": extras,
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
            "explicit_topic" if topic else "complete_story_generation"
        ),
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
        "mapping_version": MAPPING_VERSION,
        "dynamic_short_parts": bool(dynamic_short),
        "source_fingerprint": fingerprint(source_payload),
        "source": {
            "input_file": "Input/topic.txt",
            "story_text_provided": True,
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
    expected_fields = {
        "scene_id": expected_id,
        "role": kind,
        "kind": kind,
        "part": part_number,
        "scene": None,
        "global_scene_number": None,
        "separate_from_main_scenes": True,
        "main_scene_numbering_affected": False,
    }

    for field, expected in expected_fields.items():
        if record.get(field) != expected:
            raise RuntimeError(
                f"Incorrect {field} in {expected_id}: "
                f"expected {expected!r}, got {record.get(field)!r}."
            )

    if bool(record.get("is_final_part")) != bool(final_part):
        raise RuntimeError(
            f"Incorrect final-part flag in {expected_id}."
        )

    required_text = (
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
    )
    for field in required_text:
        if not clean_text(record.get(field)):
            raise RuntimeError(
                f"Missing {field} in {expected_id}."
            )


def validate_plan(
    plan: dict,
    format_type: str,
    topic: str,
    parts_count: int,
    scenes_per_part: int,
) -> None:
    """Validate main scenes, role separation, ordering and plan metadata."""
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
    if not 1 <= parts_count <= MAX_PARTS:
        raise RuntimeError("Invalid plan part count.")
    if scenes_per_part < 1:
        raise RuntimeError("Invalid scenes-per-part count.")

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
        "mapping_version": MAPPING_VERSION,
    }

    for field, expected in expected_values.items():
        if plan.get(field) != expected:
            raise RuntimeError(
                f"Story plan mismatch for {field}: "
                f"expected {expected!r}, got {plan.get(field)!r}."
            )

    if clean_text(plan.get("title")) != topic:
        raise RuntimeError("Story plan title does not match configured topic.")

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
    if source.get("story_text_provided") is not True:
        raise RuntimeError("Story plan was not built from complete story text.")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(
            f"Story plan must contain exactly {parts_count} parts."
        )

    total_hooks = 0
    total_suspense = 0
    total_resolutions = 0
    global_number = 0
    all_main_anchors = []

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid plan part {part_number}.")

        expected_part_values = {
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "main_scene_count": scenes_per_part,
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

        if not clean_text(part.get("story_anchor")):
            raise RuntimeError(f"Part {part_number} has no story anchor.")

        part_anchors = []
        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            expected_id = (
                f"part_{part_number:02d}_scene_{scene_number:02d}"
            )

            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid main scene: {expected_id}.")

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
                        f"Incorrect {field} in {expected_id}: "
                        f"expected {expected!r}, got {scene.get(field)!r}."
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
                        f"Missing required {field} in {expected_id}."
                    )

            anchor = clean_text(scene.get("story_anchor"))
            part_anchors.append(anchor)
            all_main_anchors.append(anchor)

        if clean_text(" ".join(part_anchors)) != clean_text(
            part.get("story_anchor")
        ):
            raise RuntimeError(
                f"Part {part_number} story_anchor does not match its main scenes."
            )

        is_final = part_number == parts_count
        expected_hook = True
        expected_suspense = not is_final
        expected_resolution = is_final

        expected_flags = {
            "hook_required": expected_hook,
            "suspense_required": expected_suspense,
            "final_resolution": expected_resolution,
        }
        for field, expected in expected_flags.items():
            if part.get(field) is not expected:
                raise RuntimeError(
                    f"Part {part_number} has incorrect {field} flag."
                )

        hook = part.get("hook_scene")
        suspense = part.get("suspense_scene")
        resolution = part.get("resolution_scene")

        validate_extra_scene(
            hook, part_number, "hook", True, is_final
        )
        validate_extra_scene(
            suspense, part_number, "suspense", expected_suspense, False
        )
        validate_extra_scene(
            resolution, part_number, "resolution", expected_resolution, True
        )

        expected_extras = [
            item for item in (hook, suspense, resolution)
            if isinstance(item, dict)
        ]
        extras = part.get("extra_scenes")

        if not isinstance(extras, list) or extras != expected_extras:
            raise RuntimeError(
                f"Part {part_number} extra_scenes does not match its "
                "separate Hook/Suspense/Resolution records."
            )

        if clean_text(hook.get("story_anchor")) != clean_text(part_anchors[0]):
            raise RuntimeError(
                f"Part {part_number} Hook is not anchored to its opening scene."
            )

        if suspense is not None and clean_text(
            suspense.get("story_anchor")
        ) != clean_text(part_anchors[-1]):
            raise RuntimeError(
                f"Part {part_number} Suspense is not anchored to its ending scene."
            )

        if resolution is not None and clean_text(
            resolution.get("story_anchor")
        ) != clean_text(part_anchors[-1]):
            raise RuntimeError(
                "Final Resolution is not anchored to the final main scene."
            )

        total_hooks += int(hook is not None)
        total_suspense += int(suspense is not None)
        total_resolutions += int(resolution is not None)

    if global_number != expected_total:
        raise RuntimeError("Main scene global numbering is incorrect.")

    expected_hooks = parts_count
    expected_suspense = max(0, parts_count - 1)
    expected_resolutions = 1
    expected_extras = expected_hooks + expected_suspense + expected_resolutions

    metadata = {
        "total_scenes": expected_total,
        "total_main_scenes": expected_total,
        "total_hook_scenes": expected_hooks,
        "total_suspense_scenes": expected_suspense,
        "total_resolution_scenes": expected_resolutions,
        "total_extra_scenes": expected_extras,
    }

    for field, expected in metadata.items():
        try:
            actual = int(plan.get(field, -1))
        except (TypeError, ValueError):
            actual = -1
        if actual != expected:
            raise RuntimeError(
                f"Plan metadata mismatch for {field}: "
                f"expected {expected}, got {actual}."
            )

    if (total_hooks, total_suspense, total_resolutions) != (
        expected_hooks,
        expected_suspense,
        expected_resolutions,
    ):
        raise RuntimeError("Separate Hook/Suspense/Resolution counts are invalid.")

    if len(all_main_anchors) != expected_total:
        raise RuntimeError("Main scene anchor count is invalid.")

    if any(not anchor for anchor in all_main_anchors):
        raise RuntimeError("One or more main scenes have empty story anchors.")

    if word_count(" ".join(all_main_anchors)) != int(
        source.get("story_word_count", -1)
    ):
        raise RuntimeError(
            "Main scene anchors do not preserve the complete source story word count."
        )


def read_story_text_direct(path: Path) -> str:
    """Read the complete multiline STORY_TEXT assignment."""
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

        if len(value) >= 2 and value[0] == value[-1]:
            if value[0] in {"'", '"'}:
                return value[1:-1].strip()
        return value

    return ""


def get_complete_input_story(config: dict, path: Path) -> str:
    """Prefer the complete direct multiline input if parsing truncated it."""
    parsed = normalize_text(get_story_text(config))
    direct = normalize_text(read_story_text_direct(path))

    if word_count(direct) > word_count(parsed):
        log(
            "STORY_TEXT parser mismatch:",
            f"parsed={word_count(parsed)}",
            f"direct={word_count(direct)}",
        )
        log("Using complete direct STORY_TEXT input.")
        return direct

    if parsed and direct and word_count(parsed) != word_count(direct):
        log(
            "STORY_TEXT word-count difference:",
            f"parsed={word_count(parsed)}",
            f"direct={word_count(direct)}",
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
        parts_count = int(get_parts(config))
        scenes_per_part = int(get_scenes(config))
    except (TypeError, ValueError):
        raise RuntimeError("PARTS and SCENES must be valid integers.")

    if format_type not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {format_type!r}.")
    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")
    if parts_count > MAX_PARTS:
        raise RuntimeError(f"PARTS cannot exceed {MAX_PARTS}.")
    if parts_count * scenes_per_part > MAX_TOTAL_MAIN_SCENES:
        raise RuntimeError(
            f"Requested plan exceeds {MAX_TOTAL_MAIN_SCENES} main scenes."
        )
    if not story_text:
        raise RuntimeError(
            "A complete story is required before planning. "
            "A topic alone is not sufficient for story-grounded scenes."
        )
    if word_count(story_text) < 20:
        raise RuntimeError(
            "STORY_TEXT contains fewer than 20 words. "
            "Check the complete multiline input."
        )

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
        except (RuntimeError, TypeError, ValueError, KeyError) as exc:
            log("Existing plan is invalid; rebuilding:", exc)

    write_json(OUTPUT_FILE, plan)

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
    log("Total extra scenes:", plan["total_extra_scenes"])
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