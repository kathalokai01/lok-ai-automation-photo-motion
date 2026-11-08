#!/usr/bin/env python3
"""Build the story plan once; subsequent runs only verify it."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from input_config import (
    get_format,
    get_scenes_range,
    get_selected_parts,
    get_selected_parts_record,
    get_topic,
    get_parts_range,
    load_and_validate,
)

INPUT_FILE = ROOT / "Input" / "topic.txt"
STORY_FILE = ROOT / "output" / "story" / "story.json"
OUTPUT_FILE = ROOT / "output" / "story" / "story_plan.json"

VERSION = "story-plan-v19-extra-role-narration-pending-ai-generation"
MAPPING_VERSION = 19

MAX_PARTS = 1000
MAX_SCENES_PER_PART = 500
MAX_TOTAL_MAIN_SCENES = 1000

SENTENCE_SPLIT = re.compile(r"(?<=[।॥.!?！？…])\s+|\n+")
CLAUSE_SPLIT = re.compile(r"(?<=[,，;؛:])\s+")
TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)

DANGLING_STARTERS = {
    "और", "या", "लेकिन", "मगर", "क्योंकि", "इसलिए",
    "तथा", "अथवा", "कि", "जो", "जिससे", "जिसका",
    "जिसकी", "जिसके", "जब", "जहाँ", "ताकि", "वरना",
}

DANGLING_ENDINGS = {
    "और", "या", "लेकिन", "मगर", "क्योंकि", "कि", "जो",
    "जिससे", "जिसका", "जिसकी", "जिसके", "जब", "जहाँ",
    "ताकि", "से", "को", "का", "की", "के", "में", "पर",
    "तक", "लिए", "साथ",
}

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
    ("approach", "Move toward the established central conflict."),
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


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    normalized = normalize_text(text)
    return [
        clean_text(piece)
        for piece in SENTENCE_SPLIT.split(normalized)
        if clean_text(piece)
    ]


def split_clauses(sentence: str) -> list[str]:
    sentence = clean_text(sentence)
    if not sentence:
        return []

    pieces = [
        clean_text(piece)
        for piece in CLAUSE_SPLIT.split(sentence)
        if clean_text(piece)
    ]

    if len(pieces) < 2:
        return [sentence]

    for piece in pieces:
        tokens = TOKEN_RE.findall(piece.lower())
        if len(tokens) < 3:
            return [sentence]
        if tokens[0] in DANGLING_STARTERS:
            return [sentence]
        if tokens[-1] in DANGLING_ENDINGS:
            return [sentence]

    return pieces


def split_natural_units(text: str, required_count: int) -> list[str]:
    if isinstance(required_count, bool) or int(required_count) < 1:
        raise RuntimeError("Required Scene count must be positive.")

    sentences = split_sentences(text)
    if not sentences:
        raise RuntimeError("Complete story contains no usable sentences.")

    units = list(sentences)

    if len(units) < int(required_count):
        units = []
        for sentence in sentences:
            units.extend(split_clauses(sentence))

    if len(units) < int(required_count):
        raise RuntimeError(
            f"Only {len(units)} complete sentence/clause units exist; "
            f"{required_count} Scenes are required. "
            "Mid-sentence word splitting is forbidden."
        )

    return units


def group_natural_units(units: list[str], count: int) -> list[str]:
    if count < 1 or len(units) < count:
        raise RuntimeError("Insufficient natural units for Scene mapping.")

    groups = []
    cursor = 0
    remaining_words = sum(word_count(unit) for unit in units)

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
            size = word_count(candidate)

            if selected and abs(selected_words - target) <= abs(
                selected_words + size - target
            ):
                break

            selected.append(candidate)
            selected_words += size
            cursor += 1

            if selected_words >= target:
                break

        if not selected:
            raise RuntimeError("Unable to create a non-empty Scene.")

        groups.append(" ".join(selected))
        remaining_words -= selected_words

    if cursor != len(units) or len(groups) != count:
        raise RuntimeError("Natural Scene grouping did not finish correctly.")

    if clean_text(" ".join(units)) != clean_text(" ".join(groups)):
        raise RuntimeError("Scene grouping changed or reordered story text.")

    return groups


def canonical_main_id(part_number: int, scene_number: int) -> str:
    return f"part_{part_number:02d}_scene_{scene_number:02d}"


def canonical_extra_id(part_number: int, role: str) -> str:
    return f"part_{part_number:02d}_{role}"


def select_beat(
    format_type: str,
    index: int,
    total: int,
) -> tuple[str, str]:
    beats = SHORT_BEATS if format_type == "short" else FULL_BEATS
    if total <= 1:
        return beats[0]
    position = round(index * (len(beats) - 1) / (total - 1))
    return beats[max(0, min(position, len(beats) - 1))]


def _coerce_scene_range(
    config: dict,
    scenes_per_part: Any,
) -> tuple[int, int]:
    configured = get_scenes_range(config)

    if isinstance(scenes_per_part, (tuple, list)):
        if len(scenes_per_part) != 2:
            raise RuntimeError("SCENES range must contain exactly two values.")
        if any(
            isinstance(value, bool) or not str(value).strip().isdigit()
            for value in scenes_per_part
        ):
            raise RuntimeError("SCENES range values must be positive integers.")
        supplied = (int(scenes_per_part[0]), int(scenes_per_part[1]))
        if supplied[0] < 1 or supplied[1] < supplied[0]:
            raise RuntimeError("SCENES range is reversed or invalid.")
        if supplied != configured:
            raise RuntimeError(
                "Passed SCENES range does not match Input/topic.txt."
            )
    elif scenes_per_part is not None:
        if (
            isinstance(scenes_per_part, bool)
            or not str(scenes_per_part).strip().isdigit()
        ):
            raise RuntimeError("Invalid SCENES minimum.")
        if int(scenes_per_part) != configured[0]:
            raise RuntimeError(
                "Legacy SCENES argument does not match Input/topic.txt."
            )

    minimum, maximum = configured
    if not 1 <= minimum <= maximum <= MAX_SCENES_PER_PART:
        raise RuntimeError("SCENES range is outside supported limits.")

    return minimum, maximum


def _load_part_texts(
    story_text: str,
    parts_count: int,
    part_texts: Iterable[str] | None,
) -> list[str]:
    if part_texts is not None:
        result = [normalize_text(item) for item in part_texts]
        if len(result) != parts_count:
            raise RuntimeError(
                f"Received {len(result)} Parts; expected {parts_count}."
            )
    else:
        cached = read_json(STORY_FILE)
        cached_parts = cached.get("parts") if cached else None
        cached_story = normalize_text(
            (cached or {}).get("corrected_complete_story")
            or (cached or {}).get("story_text")
        )

        if (
            isinstance(cached_parts, list)
            and len(cached_parts) == parts_count
            and clean_text(cached_story) == clean_text(story_text)
        ):
            result = [
                normalize_text(row.get("narration"))
                for row in cached_parts
                if isinstance(row, dict)
            ]
        else:
            units = split_sentences(story_text)
            if len(units) < parts_count:
                expanded = []
                for sentence in units:
                    expanded.extend(split_clauses(sentence))
                units = expanded
            result = group_natural_units(units, parts_count)

    if len(result) != parts_count or any(not clean_text(x) for x in result):
        raise RuntimeError("One or more Part narrations are invalid.")

    if clean_text(" ".join(result)) != clean_text(story_text):
        raise RuntimeError(
            "Part narrations do not reconstruct the complete story."
        )

    return result


def _rebalance_part_units(
    part_texts: list[str],
    minimum: int,
    maximum: int,
) -> tuple[list[list[str]], list[dict]]:
    all_groups = []
    distribution = []
    carry = []
    source_number = 0

    for part_index, original_text in enumerate(part_texts):
        part_number = part_index + 1
        own_rows = []

        for sentence in split_sentences(original_text):
            source_number += 1
            own_rows.append({
                "text": sentence,
                "source_sentence": source_number,
                "source_part": part_number,
            })

        if not own_rows:
            raise RuntimeError(f"Part {part_number} contains no sentences.")

        incoming = list(carry)
        units = incoming + own_rows
        carry = []
        is_final = part_number == len(part_texts)

        if len(units) > maximum and not is_final:
            assigned = units[:maximum]
            carry = units[maximum:]
            groups = [row["text"] for row in assigned]
        else:
            if len(units) < minimum:
                expanded = []
                for row in units:
                    for piece in split_clauses(row["text"]):
                        expanded.append({**row, "text": piece})
                units = expanded

            if len(units) < minimum:
                raise RuntimeError(
                    f"Part {part_number} cannot reach SCENES minimum "
                    "without splitting a sentence unnaturally."
                )

            assigned = units
            if len(units) > maximum:
                groups = group_natural_units(
                    [row["text"] for row in units],
                    maximum,
                )
            else:
                groups = [row["text"] for row in units]

        if not minimum <= len(groups) <= maximum:
            raise RuntimeError(
                f"Part {part_number} has {len(groups)} Scenes; "
                f"configured range is {minimum}-{maximum}."
            )

        assigned_text = clean_text(" ".join(row["text"] for row in assigned))
        if clean_text(" ".join(groups)) != assigned_text:
            raise RuntimeError(
                f"Part {part_number} Scene groups changed assigned text."
            )

        all_groups.append(groups)
        distribution.append({
            "part": part_number,
            "original_word_count": word_count(original_text),
            "rebalanced_word_count": word_count(assigned_text),
            "scene_count": len(groups),
            "carried_in_sentence_numbers": [
                row["source_sentence"] for row in incoming
            ],
            "carried_out_sentence_numbers": [
                row["source_sentence"] for row in carry
            ],
            "carried_in_count": len(incoming),
            "carried_out_count": len(carry),
            "source_sentence_numbers": [
                row["source_sentence"] for row in assigned
            ],
        })

    if carry:
        raise RuntimeError("Story sentences remain unassigned after final Part.")

    original = clean_text(" ".join(part_texts))
    rebuilt = clean_text(
        " ".join(scene for group in all_groups for scene in group)
    )
    if original != rebuilt:
        raise RuntimeError(
            "Scene planning omitted, duplicated or reordered story text."
        )

    return all_groups, distribution


def _make_main_scene(
    format_type: str,
    part_number: int,
    scene_number: int,
    global_number: int,
    total_scenes: int,
    anchor: str,
) -> dict:
    anchor = clean_text(anchor)
    if not anchor:
        raise RuntimeError("Main Scene has no story anchor.")

    beat, description = select_beat(
        format_type,
        global_number - 1,
        total_scenes,
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
        "narration": anchor,
        "source_text_sha256": sha256_text(anchor),
        "source_word_count": word_count(anchor),
        "continuity_rule": (
            "Maintain established character identity, appearance, "
            "clothing, location, objects and chronology."
        ),
        "invention_policy": (
            "Do not invent unsupported characters, relationships, "
            "locations, objects, events, dialogue, clues or twists."
        ),
        "visual_policy": (
            "Depict the exact narrated story moment. Avoid unrelated "
            "stock scenes or generic imagery."
        ),
    }


def _make_extra_scene(
    part_number: int,
    role: str,
    anchor: str,
    format_type: str,
    final_part: bool,
) -> dict:
    purposes = {
        "hook": (
            "Create a story-specific opening that raises curiosity "
            "using established story facts."
        ),
        "suspense": (
            "Create an unresolved question from this non-final Part "
            "that leads naturally into the next Part."
        ),
        "resolution": (
            "Deliver the actual complete-story resolution using "
            "the established ending."
        ),
    }

    if role not in purposes:
        raise RuntimeError(f"Unsupported extra Scene role: {role}")

    anchor = clean_text(anchor)
    if not anchor:
        raise RuntimeError(f"Part {part_number} {role} anchor is empty.")

    return {
        "part": part_number,
        "scene": None,
        "global_scene_number": None,
        "scene_id": canonical_extra_id(part_number, role),
        "role": role,
        "kind": role,
        "title": f"भाग {part_number} — {role.title()}",
        "beat": role,
        "beat_description": purposes[role],
        "purpose": purposes[role],
        "story_anchor": anchor,
        "narration": "",
        "voiceover_enabled": False,
        "narration_source": "pending_ai_narration",
        "source_text_sha256": "",
        "source_word_count": 0,
        "source_format": format_type,
        "is_final_part": bool(final_part),
        "separate_from_main_scenes": True,
        "main_scene_numbering_affected": False,
        "instruction": purposes[role],
        "continuity_rule": (
            "Maintain established character identities, appearance, "
            "clothing, locations, objects and chronology."
        ),
        "invention_policy": (
            "Use only established story facts. Do not add unrelated "
            "characters, objects, locations, events or twists."
        ),
        "visual_policy": (
            "Depict the exact story-specific role moment. "
            "Avoid generic imagery."
        ),
    }


def build_plan(
    config: dict,
    format_type: str,
    topic: str,
    story_text: str,
    parts_count: int,
    scenes_per_part: Any,
    dynamic_short: bool = False,
    part_texts: Iterable[str] | None = None,
) -> dict:
    del dynamic_short

    mode = clean_text(format_type).lower()
    if mode not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")

    if (
        isinstance(parts_count, bool)
        or not str(parts_count).strip().isdigit()
        or not 1 <= int(parts_count) <= MAX_PARTS
    ):
        raise RuntimeError("Invalid selected PARTS count.")

    parts_count = int(parts_count)
    scenes_min, scenes_max = _coerce_scene_range(config, scenes_per_part)
    story_text = normalize_text(story_text)

    if not story_text:
        raise RuntimeError("Complete corrected story is empty.")

    source_parts = _load_part_texts(story_text, parts_count, part_texts)
    scene_groups, distribution = _rebalance_part_units(
        source_parts,
        scenes_min,
        scenes_max,
    )

    total_main = sum(len(group) for group in scene_groups)
    if total_main > MAX_TOTAL_MAIN_SCENES:
        raise RuntimeError("Total Main Scene count exceeds supported limit.")

    parts = []
    all_main = []
    all_extra = []
    global_number = 0

    for part_number, (narration, anchors) in enumerate(
        zip(source_parts, scene_groups),
        1,
    ):
        scenes = []
        for scene_number, anchor in enumerate(anchors, 1):
            global_number += 1
            scene = _make_main_scene(
                mode,
                part_number,
                scene_number,
                global_number,
                total_main,
                anchor,
            )
            scenes.append(scene)
            all_main.append(scene)

        final_part = part_number == parts_count
        hook = _make_extra_scene(
            part_number, "hook", anchors[0], mode, final_part
        )
        suspense = (
            None if final_part else _make_extra_scene(
                part_number, "suspense", anchors[-1], mode, False
            )
        )
        resolution = (
            _make_extra_scene(
                part_number, "resolution", anchors[-1], mode, True
            ) if final_part else None
        )

        extras = [
            row for row in (hook, suspense, resolution)
            if isinstance(row, dict)
        ]

        locked_part_text = clean_text(narration)
        locked_part_hash = sha256_text(locked_part_text)
        parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": f"भाग {part_number}",
            "narration": locked_part_text,
            "story_anchor": locked_part_text,
            "word_count": word_count(locked_part_text),
            "text_locked": True,
            "locked_word_count": word_count(locked_part_text),
            "locked_text_sha256": locked_part_hash,
            "actual_scene_count": len(scenes),
            "main_scene_count": len(scenes),
            "scenes": scenes,
            "hook_scene": hook,
            "suspense_scene": suspense,
            "resolution_scene": resolution,
            "extra_scenes": extras,
            "hook_required": True,
            "suspense_required": not final_part,
            "final_resolution": final_part,
        })
        all_extra.extend(extras)

    source_payload = {
        "format": mode,
        "topic": clean_text(topic),
        "story_text": clean_text(story_text),
        "parts_count": parts_count,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "part_texts": [clean_text(item) for item in source_parts],
        "actual_scene_counts": [len(group) for group in scene_groups],
        "mapping_version": MAPPING_VERSION,
    }

    plan = {
        "version": VERSION,
        "script_version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "status": "planned",
        "format": mode,
        "topic": clean_text(topic),
        "parts_count": parts_count,
        "total_parts": parts_count,
        "scenes_min": scenes_min,
        "scenes_max": scenes_max,
        "scenes_range": [scenes_min, scenes_max],
        "actual_scene_counts": [len(group) for group in scene_groups],
        "total_main_scenes": len(all_main),
        "total_scenes": len(all_main),
        "total_hook_scenes": parts_count,
        "total_suspense_scenes": max(0, parts_count - 1),
        "total_resolution_scenes": 1,
        "total_extra_scenes": len(all_extra),
        "scene_order": "hook_then_main_then_suspense_or_resolution",
        "main_scene_order": "part_then_scene",
        "parts": parts,
        "scenes": all_main,
        "extra_scenes": all_extra,
        "part_distribution": distribution,
        "source": {
            "api_calls": 0,
            "planning_mode": "final_corrected_story",
            "story_text_provided": True,
            "story_word_count": word_count(story_text),
            "story_sha256": sha256_text(clean_text(story_text)),
        },
        "source_payload": source_payload,
        "source_fingerprint": fingerprint(source_payload),
    }

    validate_plan(plan, mode, topic, parts_count, (scenes_min, scenes_max))
    return plan


def validate_plan(
    plan: dict,
    format_type: str,
    topic: str,
    parts_count: int,
    scenes_range: Any,
) -> bool:
    if not isinstance(plan, dict):
        raise RuntimeError("Story Plan is not an object.")
    if plan.get("status") != "planned":
        raise RuntimeError("Story Plan status is not planned.")
    if clean_text(plan.get("format")).lower() != clean_text(format_type).lower():
        raise RuntimeError("Story Plan FORMAT mismatch.")
    if clean_text(plan.get("topic")) != clean_text(topic):
        raise RuntimeError("Story Plan topic mismatch.")
    if int(plan.get("parts_count", -1)) != int(parts_count):
        raise RuntimeError("Story Plan PARTS count mismatch.")
    if clean_text(plan.get("script_version")) != VERSION:
        raise RuntimeError("Story Plan script version mismatch.")
    if int(plan.get("mapping_version", -1)) != MAPPING_VERSION:
        raise RuntimeError("Story Plan mapping version mismatch.")
    if plan.get("scene_order") != "hook_then_main_then_suspense_or_resolution":
        raise RuntimeError("Story Plan scene order is invalid.")
    if plan.get("main_scene_order") != "part_then_scene":
        raise RuntimeError("Story Plan Main Scene order is invalid.")

    if isinstance(scenes_range, (tuple, list)):
        if len(scenes_range) != 2 or any(
            isinstance(value, bool) or not str(value).strip().isdigit()
            for value in scenes_range
        ):
            raise RuntimeError("SCENES range must contain two positive integers.")
        minimum, maximum = int(scenes_range[0]), int(scenes_range[1])
    else:
        if isinstance(scenes_range, bool) or not str(scenes_range).strip().isdigit():
            raise RuntimeError("SCENES count must be a positive integer.")
        minimum = maximum = int(scenes_range)

    if not 1 <= minimum <= maximum <= MAX_SCENES_PER_PART:
        raise RuntimeError("Story Plan SCENES range is reversed or unsupported.")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != int(parts_count):
        raise RuntimeError("Story Plan Parts list is invalid.")

    all_main_ids = set()
    all_extra_ids = set()
    all_anchors = []
    main_count = 0
    expected_global_scene = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid Part {part_number}.")
        if int(part.get("part", -1)) != part_number:
            raise RuntimeError(f"Part {part_number} numbering mismatch.")
        if clean_text(part.get("part_id")) != f"part_{part_number:02d}":
            raise RuntimeError(f"Part {part_number} ID mismatch.")

        narration = clean_text(part.get("narration"))
        scenes = part.get("scenes")
        if not narration:
            raise RuntimeError(f"Part {part_number} narration is empty.")
        if not isinstance(scenes, list) or not minimum <= len(scenes) <= maximum:
            raise RuntimeError(
                f"Part {part_number} Scene count is outside {minimum}-{maximum}."
            )
        if part.get("main_scene_count") != len(scenes):
            raise RuntimeError(f"Part {part_number} Main Scene count mismatch.")
        if part.get("actual_scene_count") != len(scenes):
            raise RuntimeError(f"Part {part_number} actual Scene count mismatch.")
        if part.get("word_count") != word_count(narration):
            raise RuntimeError(f"Part {part_number} word count mismatch.")
        if part.get("text_locked") is not True:
            raise RuntimeError(f"Part {part_number} text is not locked.")
        if part.get("locked_word_count") != word_count(narration):
            raise RuntimeError(f"Part {part_number} locked word count mismatch.")
        if part.get("locked_text_sha256") != sha256_text(narration):
            raise RuntimeError(f"Part {part_number} locked text hash mismatch.")

        final_part = part_number == len(parts)
        if part.get("hook_required") is not True:
            raise RuntimeError(f"Part {part_number} Hook requirement mismatch.")
        if part.get("suspense_required") is not (not final_part):
            raise RuntimeError(f"Part {part_number} Suspense requirement mismatch.")
        if part.get("final_resolution") is not final_part:
            raise RuntimeError(f"Part {part_number} Resolution requirement mismatch.")

        part_anchors = []
        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError("Invalid Main Scene record.")

            expected_global_scene += 1
            expected_id = canonical_main_id(part_number, scene_number)
            if scene.get("scene_id") != expected_id:
                raise RuntimeError(f"Main Scene ID mismatch: {expected_id}.")
            if scene.get("role") != "main":
                raise RuntimeError(f"Invalid Main Scene role: {expected_id}.")
            if scene.get("part") != part_number:
                raise RuntimeError(f"Main Scene Part mismatch: {expected_id}.")
            if scene.get("scene") != scene_number:
                raise RuntimeError(f"Main Scene number mismatch: {expected_id}.")
            if scene.get("global_scene_number") != expected_global_scene:
                raise RuntimeError(
                    f"Main Scene global order mismatch: {expected_id}."
                )

            for field in (
                "beat", "beat_description", "purpose", "continuity_rule",
                "invention_policy", "visual_policy",
            ):
                if not clean_text(scene.get(field)):
                    raise RuntimeError(
                        f"Main Scene {expected_id} missing {field}."
                    )

            anchor = clean_text(scene.get("story_anchor"))
            if not anchor or clean_text(scene.get("narration")) != anchor:
                raise RuntimeError(f"Invalid Scene narration: {expected_id}.")
            if scene.get("source_text_sha256") != sha256_text(anchor):
                raise RuntimeError(f"Scene source text hash mismatch: {expected_id}.")
            if scene.get("source_word_count") != word_count(anchor):
                raise RuntimeError(f"Scene source word count mismatch: {expected_id}.")
            if expected_id in all_main_ids:
                raise RuntimeError(f"Duplicate Main Scene ID: {expected_id}")

            all_main_ids.add(expected_id)
            part_anchors.append(anchor)
            all_anchors.append(anchor)

        if clean_text(" ".join(part_anchors)) != narration:
            raise RuntimeError(
                f"Main Scenes do not reconstruct Part {part_number}."
            )

        expected_roles = (
            ("hook_scene", "hook", True),
            ("suspense_scene", "suspense", not final_part),
            ("resolution_scene", "resolution", final_part),
        )
        expected_extras = []

        for field, role, required in expected_roles:
            record = part.get(field)
            if not required:
                if record is not None:
                    raise RuntimeError(
                        f"Part {part_number} has unexpected {role} record."
                    )
                continue

            if not isinstance(record, dict):
                raise RuntimeError(
                    f"Part {part_number} is missing {role} record."
                )

            expected_id = canonical_extra_id(part_number, role)
            if record.get("scene_id") != expected_id:
                raise RuntimeError(f"Invalid extra Scene ID: {expected_id}.")
            if record.get("role") != role or record.get("kind") != role:
                raise RuntimeError(f"Invalid extra Scene role: {expected_id}.")
            if record.get("part") != part_number:
                raise RuntimeError(f"Invalid extra Scene Part: {expected_id}.")
            if record.get("scene") is not None:
                raise RuntimeError(f"Extra Scene number must be null: {expected_id}.")
            if record.get("global_scene_number") is not None:
                raise RuntimeError(
                    f"Extra Scene global number must be null: {expected_id}."
                )
            if record.get("separate_from_main_scenes") is not True:
                raise RuntimeError(f"Extra Scene must be separate: {expected_id}.")
            if record.get("main_scene_numbering_affected") is not False:
                raise RuntimeError(
                    f"Extra Scene must not affect Main numbering: {expected_id}."
                )
            if record.get("is_final_part") is not final_part:
                raise RuntimeError(
                    f"Extra Scene final-Part flag mismatch: {expected_id}."
                )
            if record.get("source_format") != clean_text(format_type).lower():
                raise RuntimeError(f"Extra Scene format mismatch: {expected_id}.")
            if clean_text(record.get("narration")):
                raise RuntimeError(f"Extra Scene narration must remain pending before AI generation: {expected_id}.")
            if record.get("voiceover_enabled") is not False:
                raise RuntimeError(f"Extra Scene voiceover must remain disabled before AI generation: {expected_id}.")
            if record.get("narration_source") != "pending_ai_narration":
                raise RuntimeError(f"Extra Scene must be marked pending_ai_narration: {expected_id}.")
            if record.get("source_text_sha256") or record.get("source_word_count") != 0:
                raise RuntimeError(f"Pending extra Scene must not carry narration text metadata: {expected_id}.")

            for required_field in (
                "title", "beat", "beat_description", "purpose",
                "story_anchor", "instruction", "continuity_rule",
                "invention_policy", "visual_policy",
            ):
                if not clean_text(record.get(required_field)):
                    raise RuntimeError(
                        f"Extra Scene {expected_id} missing {required_field}."
                    )

            if expected_id in all_extra_ids or expected_id in all_main_ids:
                raise RuntimeError(f"Duplicate extra Scene ID: {expected_id}")

            all_extra_ids.add(expected_id)
            expected_extras.append(record)

        if part.get("extra_scenes") != expected_extras:
            raise RuntimeError(f"Part {part_number} extra_scenes list mismatch.")

        if clean_text(part["hook_scene"]["story_anchor"]) != part_anchors[0]:
            raise RuntimeError(f"Part {part_number} Hook anchor mismatch.")

        if not final_part:
            if clean_text(part["suspense_scene"]["story_anchor"]) != part_anchors[-1]:
                raise RuntimeError(f"Part {part_number} Suspense anchor mismatch.")
        elif clean_text(part["resolution_scene"]["story_anchor"]) != part_anchors[-1]:
            raise RuntimeError("Final Resolution anchor mismatch.")

        main_count += len(scenes)

    rebuilt = clean_text(" ".join(all_anchors))
    source = plan.get("source")
    if not isinstance(source, dict):
        raise RuntimeError("Story Plan source metadata is invalid.")
    if source.get("api_calls") != 0:
        raise RuntimeError("Story planning must not make external API calls.")
    if source.get("planning_mode") != "final_corrected_story":
        raise RuntimeError("Story Plan was not built from corrected story.")
    if source.get("story_text_provided") is not True:
        raise RuntimeError("Complete corrected story text is not recorded.")
    if source.get("story_word_count") != word_count(rebuilt):
        raise RuntimeError("Story Plan source word count mismatch.")
    if source.get("story_sha256") != sha256_text(rebuilt):
        raise RuntimeError("Story Plan source hash mismatch.")

    if expected_global_scene != main_count:
        raise RuntimeError("Story Plan global Scene sequence is incomplete.")
    if plan.get("total_main_scenes") != main_count:
        raise RuntimeError("Story Plan Main Scene count mismatch.")
    if plan.get("total_scenes") != main_count:
        raise RuntimeError("Story Plan total Scene count mismatch.")
    flattened_main = [
        scene for part in parts for scene in part["scenes"]
    ]
    if plan.get("scenes") != flattened_main:
        raise RuntimeError("Story Plan flattened Main Scene list mismatch.")

    expected_counts = [len(part["scenes"]) for part in parts]
    if plan.get("actual_scene_counts") != expected_counts:
        raise RuntimeError("Story Plan actual Scene counts mismatch.")
    if plan.get("total_hook_scenes") != parts_count:
        raise RuntimeError("Story Plan Hook count mismatch.")
    if plan.get("total_suspense_scenes") != max(0, parts_count - 1):
        raise RuntimeError("Story Plan Suspense count mismatch.")
    if plan.get("total_resolution_scenes") != 1:
        raise RuntimeError("Story Plan Resolution count mismatch.")
    if plan.get("total_extra_scenes") != len(all_extra_ids):
        raise RuntimeError("Story Plan extra Scene count mismatch.")
    if plan.get("scenes_min") != minimum:
        raise RuntimeError("Story Plan minimum SCENES mismatch.")
    if plan.get("scenes_max") != maximum:
        raise RuntimeError("Story Plan maximum SCENES mismatch.")
    if plan.get("total_parts") != parts_count:
        raise RuntimeError("Story Plan total Parts count mismatch.")

    payload = plan.get("source_payload")
    if not isinstance(payload, dict):
        raise RuntimeError("Story Plan source payload is missing.")
    if payload.get("mapping_version") != MAPPING_VERSION:
        raise RuntimeError("Story Plan source mapping version mismatch.")
    if payload.get("format") != clean_text(format_type).lower():
        raise RuntimeError("Story Plan source FORMAT mismatch.")
    if clean_text(payload.get("topic")) != clean_text(topic):
        raise RuntimeError("Story Plan source topic mismatch.")
    if payload.get("parts_count") != parts_count:
        raise RuntimeError("Story Plan source PARTS mismatch.")
    if payload.get("scenes_min") != minimum or payload.get("scenes_max") != maximum:
        raise RuntimeError("Story Plan source SCENES range mismatch.")
    if payload.get("actual_scene_counts") != expected_counts:
        raise RuntimeError("Story Plan source actual Scene counts mismatch.")
    payload_parts = payload.get("part_texts")
    if not isinstance(payload_parts, list) or len(payload_parts) != parts_count:
        raise RuntimeError("Story Plan source Part texts are invalid.")
    for index, part in enumerate(parts):
        if clean_text(payload_parts[index]) != clean_text(part["narration"]):
            raise RuntimeError(f"Story Plan source Part {index + 1} mismatch.")
    if plan.get("source_fingerprint") != fingerprint(payload):
        raise RuntimeError("Story Plan source fingerprint mismatch.")

    return True

def read_story_from_final_output() -> dict | None:
    data = read_json(STORY_FILE)
    if not isinstance(data, dict) or data.get("status") != "completed":
        return None
    return data


def _get_actual_part_count(
    config: dict,
    story: dict,
    minimum: int,
    maximum: int,
) -> int:
    parts = story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Final story has no valid Parts list.")

    actual = len(parts)
    if not minimum <= actual <= maximum:
        raise RuntimeError("Final story PARTS count is outside configured range.")

    for field in (
        "selected_parts_count", "selected_parts", "parts_count", "total_parts",
    ):
        declared = story.get(field)
        if declared is not None and int(declared) != actual:
            raise RuntimeError(f"Final story {field} does not match Parts list.")

    selected = get_selected_parts(config, required=True)
    if selected is None or int(selected) != actual:
        raise RuntimeError("Persisted PARTS selection does not match final story.")

    record = get_selected_parts_record(config)
    if not isinstance(record, dict):
        raise RuntimeError("PARTS selection record is missing.")
    if int(record.get("selected_parts", -1)) != actual:
        raise RuntimeError("PARTS selection record count mismatch.")
    if int(record.get("minimum_parts", -1)) != minimum:
        raise RuntimeError("PARTS minimum does not match Input/topic.txt.")
    if int(record.get("maximum_parts", -1)) != maximum:
        raise RuntimeError("PARTS maximum does not match Input/topic.txt.")

    return actual


def _validate_final_story_parts(
    story: dict,
    story_text: str,
    parts_count: int,
    scenes_min: int,
    scenes_max: int,
) -> None:
    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Final story Parts list is inconsistent.")

    rebuilt_parts = []

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid final story Part {part_number}.")
        if int(part.get("part", -1)) != part_number:
            raise RuntimeError(f"Part {part_number} numbering mismatch.")
        if part.get("part_id") != f"part_{part_number:02d}":
            raise RuntimeError(f"Part {part_number} ID mismatch.")

        narration = clean_text(part.get("narration"))
        scenes = part.get("scenes")

        if not narration:
            raise RuntimeError(f"Part {part_number} narration is empty.")
        if int(part.get("word_count", -1)) != word_count(narration):
            raise RuntimeError(f"Part {part_number} word count mismatch.")
        if not isinstance(scenes, list) or not scenes_min <= len(scenes) <= scenes_max:
            raise RuntimeError(f"Part {part_number} Scene count is invalid.")

        scene_texts = []

        for scene_number, scene in enumerate(scenes, 1):
            expected_id = canonical_main_id(part_number, scene_number)

            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid Scene {expected_id}.")
            if scene.get("scene_id") != expected_id:
                raise RuntimeError(f"Scene ID mismatch: {expected_id}.")
            if scene.get("role") != "main":
                raise RuntimeError(f"Scene role mismatch: {expected_id}.")
            if scene.get("part") != part_number:
                raise RuntimeError(f"Scene Part mismatch: {expected_id}.")
            if scene.get("scene") != scene_number:
                raise RuntimeError(f"Scene number mismatch: {expected_id}.")

            scene_text = clean_text(scene.get("narration"))
            if not scene_text:
                raise RuntimeError(f"Scene narration is empty: {expected_id}.")

            scene_texts.append(scene_text)

        if clean_text(" ".join(scene_texts)) != narration:
            raise RuntimeError(
                f"Scene narration does not reconstruct Part {part_number}."
            )

        rebuilt_parts.append(narration)

    if clean_text(" ".join(rebuilt_parts)) != clean_text(story_text):
        raise RuntimeError(
            "Final Parts do not reconstruct the complete corrected story."
        )


def get_final_story_data(config: dict) -> tuple[str, int, int, str]:
    story = read_story_from_final_output()

    if story is None:
        raise RuntimeError(
            "Completed output/story/story.json is required before planning."
        )

    mode = clean_text(story.get("format")).lower()
    if mode != clean_text(get_format(config)).lower():
        raise RuntimeError("Final story FORMAT does not match Input/topic.txt.")

    minimum, maximum = get_parts_range(config)
    parts_count = _get_actual_part_count(
        config, story, minimum, maximum
    )

    scenes_min, scenes_max = get_scenes_range(config)

    story_text = normalize_text(
        story.get("corrected_complete_story") or story.get("story_text")
    )

    if not story_text or word_count(story_text) < 20:
        raise RuntimeError("Final corrected story is empty or too short.")

    _validate_final_story_parts(
        story,
        story_text,
        parts_count,
        scenes_min,
        scenes_max,
    )

    title = clean_text(
        story.get("final_title")
        or story.get("title")
        or get_topic(config)
    )

    if not title:
        raise RuntimeError("Final story title is missing.")

    return story_text, parts_count, scenes_min, title


def verify_plan_matches_story(
    story: dict,
    plan: dict,
    mode: str,
    topic: str,
    parts_count: int,
    scenes_range: tuple[int, int],
    story_text: str,
) -> None:
    """Read-only cross-check; never changes the saved Story Plan."""

    if not isinstance(plan, dict):
        raise RuntimeError("Existing story_plan.json is missing or invalid.")

    if plan.get("status") != "planned":
        raise RuntimeError("Existing Story Plan status is not planned.")

    if clean_text(plan.get("format")).lower() != mode:
        raise RuntimeError("Story/Plan FORMAT mismatch.")
    if clean_text(plan.get("topic")) != clean_text(topic):
        raise RuntimeError("Story/Plan topic mismatch.")
    if int(plan.get("parts_count", -1)) != parts_count:
        raise RuntimeError("Story/Plan PARTS count mismatch.")

    payload = plan.get("source_payload")

    if not isinstance(payload, dict):
        raise RuntimeError("Story Plan source payload is missing.")

    calculated_source_fingerprint = fingerprint(payload)

    if plan.get("source_fingerprint") != calculated_source_fingerprint:
        raise RuntimeError("Story Plan source fingerprint is invalid.")

    if payload.get("format") != mode:
        raise RuntimeError("Story Plan source FORMAT mismatch.")
    if clean_text(payload.get("topic")) != clean_text(topic):
        raise RuntimeError("Story Plan source topic mismatch.")
    if int(payload.get("parts_count", -1)) != parts_count:
        raise RuntimeError("Story Plan source PARTS mismatch.")
    if int(payload.get("scenes_min", -1)) != scenes_range[0]:
        raise RuntimeError("Story Plan source SCENES minimum mismatch.")
    if int(payload.get("scenes_max", -1)) != scenes_range[1]:
        raise RuntimeError("Story Plan source SCENES maximum mismatch.")
    if clean_text(payload.get("story_text")) != clean_text(story_text):
        raise RuntimeError("Story Plan was built from a different Story.")

    story_parts = story.get("parts")
    plan_parts = plan.get("parts")

    if not isinstance(story_parts, list) or not isinstance(plan_parts, list):
        raise RuntimeError("Story or Plan Parts list is invalid.")
    if len(story_parts) != parts_count or len(plan_parts) != parts_count:
        raise RuntimeError("Story/Plan Part count mismatch.")

    payload_parts = payload.get("part_texts")

    if not isinstance(payload_parts, list) or len(payload_parts) != parts_count:
        raise RuntimeError("Story Plan source Part texts are invalid.")

    actual_counts = []
    global_scene_number = 0

    for index, (story_part, plan_part) in enumerate(
        zip(story_parts, plan_parts), 1
    ):
        if not isinstance(story_part, dict) or not isinstance(plan_part, dict):
            raise RuntimeError(f"Invalid Story/Plan Part {index}.")

        story_narration = clean_text(story_part.get("narration"))
        plan_narration = clean_text(plan_part.get("narration"))

        if not story_narration or story_narration != plan_narration:
            raise RuntimeError(
                f"Part {index} narration mismatch between Story and Plan."
            )

        if clean_text(payload_parts[index - 1]) != story_narration:
            raise RuntimeError(f"Part {index} source narration mismatch.")

        story_scenes = story_part.get("scenes")
        plan_scenes = plan_part.get("scenes")

        if not isinstance(story_scenes, list) or not isinstance(plan_scenes, list):
            raise RuntimeError(f"Part {index} Scene list is invalid.")

        if len(story_scenes) != len(plan_scenes):
            raise RuntimeError(
                f"Plan/story Scene count mismatch in Part {index}."
            )

        if not scenes_range[0] <= len(plan_scenes) <= scenes_range[1]:
            raise RuntimeError(
                f"Part {index} Scene count is outside configured range."
            )

        actual_counts.append(len(plan_scenes))

        for scene_number, (story_scene, plan_scene) in enumerate(
            zip(story_scenes, plan_scenes), 1
        ):
            global_scene_number += 1
            expected_id = canonical_main_id(index, scene_number)

            if not isinstance(story_scene, dict) or not isinstance(plan_scene, dict):
                raise RuntimeError(f"Invalid Scene record: {expected_id}.")

            for row, label in (
                (story_scene, "Story"),
                (plan_scene, "Plan"),
            ):
                if row.get("scene_id") != expected_id:
                    raise RuntimeError(
                        f"{label} Scene ID mismatch: {expected_id}."
                    )
                if row.get("role") != "main":
                    raise RuntimeError(
                        f"{label} Scene role mismatch: {expected_id}."
                    )
                if row.get("part") != index or row.get("scene") != scene_number:
                    raise RuntimeError(
                        f"{label} Scene numbering mismatch: {expected_id}."
                    )
                if row.get("global_scene_number") != global_scene_number:
                    raise RuntimeError(
                        f"{label} global Scene order mismatch: {expected_id}."
                    )

            for field in (
                "narration",
                "story_anchor",
                "beat",
                "purpose",
            ):
                if clean_text(story_scene.get(field)) != clean_text(
                    plan_scene.get(field)
                ):
                    raise RuntimeError(
                        f"Story/Plan Scene {field} mismatch: {expected_id}."
                    )

    if plan.get("actual_scene_counts") != actual_counts:
        raise RuntimeError("Story Plan actual_scene_counts mismatch.")

    if payload.get("actual_scene_counts") != actual_counts:
        raise RuntimeError("Story Plan source actual_scene_counts mismatch.")

    total_main = sum(actual_counts)

    if plan.get("total_main_scenes") != total_main:
        raise RuntimeError("Story Plan total Main Scene count mismatch.")
    if plan.get("total_scenes") != total_main:
        raise RuntimeError("Story Plan total Scene count mismatch.")

    validate_plan(
        plan,
        mode,
        topic,
        parts_count,
        scenes_range,
    )

    # generate_story.py stores plan["source_fingerprint"] here.
    # It does not store fingerprint(plan).
    expected_fingerprint = clean_text(
        story.get("story_plan_fingerprint")
    )
    actual_fingerprint = clean_text(
        plan.get("source_fingerprint")
    )

    if not expected_fingerprint:
        raise RuntimeError("Story's saved Plan fingerprint is missing.")

    if expected_fingerprint != actual_fingerprint:
        raise RuntimeError(
            "Story's saved Plan source fingerprint does not match Plan file."
        )

    plan_title = clean_text(plan.get("final_title"))
    story_title = clean_text(
        story.get("final_title") or story.get("title") or topic
    )

    if plan_title and plan_title != story_title:
        raise RuntimeError("Story/Plan title mismatch.")


def main() -> None:
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Input file is missing: {INPUT_FILE}")

    config = load_and_validate(str(INPUT_FILE))
    mode = clean_text(get_format(config)).lower()
    topic = clean_text(get_topic(config))

    if mode not in {"short", "full"}:
        raise RuntimeError("Unsupported FORMAT.")

    story_text, parts_count, _scenes_min, _title = get_final_story_data(config)
    scenes_range = get_scenes_range(config)

    story = read_story_from_final_output()

    if story is None:
        raise RuntimeError("Final story disappeared before verification.")

    plan = read_json(OUTPUT_FILE)

    if plan is None:
        raise RuntimeError(
            "story_plan.json is missing or invalid. "
            "Regenerate Story and Plan together; this stage will not rebuild it."
        )

    verify_plan_matches_story(
        story=story,
        plan=plan,
        mode=mode,
        topic=topic,
        parts_count=parts_count,
        scenes_range=scenes_range,
        story_text=story_text,
    )

    log("STORY PLAN: VERIFIED")
    log("Existing Story Plan preserved; no rewrite performed.")
    log("Format:", mode)
    log("Parts:", parts_count)
    log("Scene range:", f"{scenes_range[0]}-{scenes_range[1]}")
    log("Actual Scene counts:", plan["actual_scene_counts"])
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