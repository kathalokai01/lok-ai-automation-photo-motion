#!/usr/bin/env python3
"""Build and validate a variable-scene story plan from the final corrected story."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from input_config import (
    get_format,
    get_scenes,
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

VERSION = "story-plan-v13-variable-scenes-natural-boundaries-carry-forward"
MAPPING_VERSION = 9
MAX_PARTS = 1000
MAX_SCENES_PER_PART = 500
MAX_TOTAL_MAIN_SCENES = 1000

SENTENCE_SPLIT = re.compile(r"(?<=[।॥.!?！？…])\s+|\n+")
CLAUSE_SPLIT = re.compile(r"(?<=[,，;؛:])\s+")
TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)
DANGLING_STARTERS = {
    "और", "या", "लेकिन", "मगर", "क्योंकि", "इसलिए", "तथा", "अथवा",
    "कि", "जो", "जिससे", "जिसका", "जिसकी", "जिसके", "जब", "जहाँ",
    "ताकि", "वरना", "परंतु", "मगर", "हालाँकि", "हालांकि",
}
DANGLING_ENDINGS = {
    "और", "या", "लेकिन", "मगर", "क्योंकि", "कि", "जो", "जिससे",
    "जिसका", "जिसकी", "जिसके", "जब", "जहाँ", "ताकि", "से", "को",
    "का", "की", "के", "में", "पर", "तक", "लिए", "साथ",
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
        data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
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
    """Split only at complete sentence endings or explicit paragraph breaks."""
    normalized = normalize_text(text)
    return [clean_text(piece) for piece in SENTENCE_SPLIT.split(normalized)
            if clean_text(piece)]


def split_clauses(sentence: str) -> list[str]:
    """Return meaningful punctuation-delimited clauses; never split by words."""
    sentence = clean_text(sentence)
    if not sentence:
        return []
    pieces = [clean_text(piece) for piece in CLAUSE_SPLIT.split(sentence)
              if clean_text(piece)]
    if len(pieces) < 2:
        return [sentence]
    for piece in pieces:
        tokens = TOKEN_RE.findall(piece.lower())
        if len(tokens) < 3:
            return [sentence]
        if tokens[0] in DANGLING_STARTERS or tokens[-1] in DANGLING_ENDINGS:
            return [sentence]
    return pieces


def split_natural_units(text: str, required_count: int) -> list[str]:
    """Keep complete sentences unless clause boundaries are needed for the minimum."""
    if isinstance(required_count, bool) or int(required_count) < 1:
        raise RuntimeError("Required natural Scene count must be positive.")
    sentences = split_sentences(text)
    if not sentences:
        raise RuntimeError("Complete story contains no usable sentences.")
    units = list(sentences)
    if len(units) < int(required_count):
        expanded: list[str] = []
        for sentence in sentences:
            expanded.extend(split_clauses(sentence))
        units = expanded
    if len(units) < int(required_count):
        raise RuntimeError(
            f"Only {len(units)} complete sentence/meaningful-clause units "
            f"are available for the minimum {required_count} Scenes. "
            "Mid-sentence word splitting is not permitted."
        )
    return units


def group_natural_units(units: list[str], count: int) -> list[str]:
    """Group ordered complete units by relative word weight without loss."""
    if count < 1 or len(units) < count:
        raise RuntimeError("Insufficient natural units for Scene mapping.")
    groups: list[str] = []
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
        selected: list[str] = []
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
            raise RuntimeError("Unable to map a non-empty Scene anchor.")
        groups.append(" ".join(selected))
        remaining_words -= selected_words
    if cursor != len(units) or len(groups) != count:
        raise RuntimeError("Natural Scene grouping did not finish correctly.")
    if clean_text(" ".join(units)) != clean_text(" ".join(groups)):
        raise RuntimeError("Scene grouping lost, duplicated or reordered text.")
    if any(not clean_text(group) for group in groups):
        raise RuntimeError("One or more Scene anchors are empty.")
    return groups


def story_anchors(story_text: str, topic: str, total_scenes: int) -> list[str]:
    """Backward-compatible helper for callers that explicitly request a total."""
    if total_scenes < 1:
        raise RuntimeError("Total main Scene count must be positive.")
    text = normalize_text(story_text)
    if not text:
        raise RuntimeError("Complete corrected story is required.")
    units = split_natural_units(text, total_scenes)
    anchors = group_natural_units(units, total_scenes)
    if word_count(" ".join(anchors)) != word_count(text):
        raise RuntimeError("Scene anchors do not preserve the complete story.")
    return anchors


def select_beat(format_type: str, index: int, total: int) -> tuple[str, str]:
    beats = SHORT_BEATS if format_type == "short" else FULL_BEATS
    if total <= 1:
        return beats[0]
    position = round(index * (len(beats) - 1) / (total - 1))
    return beats[max(0, min(position, len(beats) - 1))]


def canonical_main_id(part_number: int, scene_number: int) -> str:
    return f"part_{part_number:02d}_scene_{scene_number:02d}"


def canonical_extra_id(part_number: int, role: str) -> str:
    return f"part_{part_number:02d}_{role}"


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(value)
            if len(token) > 2}


def _should_group_short_pair(left: str, right: str) -> bool:
    """Group only very short adjacent sentences with a clear textual link."""
    left_words = word_count(left)
    right_words = word_count(right)
    if left_words > 5 or right_words > 5:
        return False
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    overlap = left_tokens.intersection(right_tokens)
    right_first = TOKEN_RE.findall(right.lower())
    linked_start = bool(right_first and right_first[0] in DANGLING_STARTERS)
    return bool(overlap or linked_start)


def _group_short_related(units: list[str], minimum: int) -> list[str]:
    groups: list[str] = []
    index = 0
    while index < len(units):
        if (index + 1 < len(units)
                and len(units) - 1 >= minimum
                and _should_group_short_pair(units[index], units[index + 1])):
            groups.append(units[index] + " " + units[index + 1])
            index += 2
        else:
            groups.append(units[index])
            index += 1
    return groups


def _expand_rows_to_minimum(rows: list[dict], minimum: int) -> list[dict]:
    """Split only meaningful clauses while retaining source sentence identity."""
    result = [dict(row) for row in rows]
    while len(result) < minimum:
        candidate_index = -1
        candidate_parts: list[str] = []
        candidate_weight = -1
        for index, row in enumerate(result):
            pieces = split_clauses(row["text"])
            if len(pieces) > 1:
                weight = sum(word_count(piece) for piece in pieces)
                if weight > candidate_weight:
                    candidate_index = index
                    candidate_parts = pieces
                    candidate_weight = weight
        if candidate_index < 0:
            break
        source = result[candidate_index]
        clause_rows = [
            {**source, "text": piece, "clause": clause_index}
            for clause_index, piece in enumerate(candidate_parts, 1)
        ]
        result[candidate_index:candidate_index + 1] = clause_rows
    if len(result) < minimum:
        raise RuntimeError(
            f"A Part has only {len(result)} complete sentence/clause units; "
            f"SCENES minimum is {minimum}. No safe natural split exists."
        )
    return result


def _split_story_into_parts(story_text: str, parts_count: int) -> list[str]:
    """Compatibility fallback; prefer exact final part narrations from story.json."""
    sentences = split_sentences(story_text)
    if len(sentences) < parts_count:
        expanded: list[str] = []
        for sentence in sentences:
            expanded.extend(split_clauses(sentence))
        sentences = expanded
    if len(sentences) < parts_count:
        raise RuntimeError(
            "Complete story has too few complete sentence/clause units "
            "to form the selected number of Parts."
        )
    return group_natural_units(sentences, parts_count)


def _coerce_scene_range(
    config: dict,
    scenes_per_part: Any,
) -> tuple[int, int]:
    configured = get_scenes_range(config)
    if isinstance(scenes_per_part, (tuple, list)):
        if len(scenes_per_part) != 2:
            raise RuntimeError("SCENES range must contain minimum and maximum.")
        try:
            supplied = (int(scenes_per_part[0]), int(scenes_per_part[1]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("SCENES bounds must be integers.") from exc
        if supplied != configured:
            raise RuntimeError(
                f"Passed SCENES range {supplied[0]}-{supplied[1]} does not "
                f"match Input/topic.txt {configured[0]}-{configured[1]}."
            )
    elif scenes_per_part is not None:
        if isinstance(scenes_per_part, bool):
            raise RuntimeError("SCENES minimum must be a positive integer.")
        try:
            compatibility_value = int(scenes_per_part)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("SCENES minimum must be an integer.") from exc
        if compatibility_value != configured[0]:
            raise RuntimeError(
                "The legacy scenes_per_part argument must equal the "
                "configured SCENES minimum; variable consumers must use "
                "get_scenes_range()."
            )
    minimum, maximum = configured
    if not 1 <= minimum <= maximum <= MAX_SCENES_PER_PART:
        raise RuntimeError("SCENES range is outside supported limits.")
    return minimum, maximum


def _load_initial_part_texts(
    story_text: str,
    parts_count: int,
    part_texts: Iterable[str] | None,
) -> list[str]:
    if part_texts is not None:
        result = [normalize_text(part) for part in part_texts]
        if len(result) != parts_count:
            raise RuntimeError(
                f"Received {len(result)} Part narrations; expected {parts_count}."
            )
    else:
        cached = read_json(STORY_FILE)
        cached_parts = cached.get("parts") if isinstance(cached, dict) else None
        cached_story = normalize_text(
            (cached or {}).get("corrected_complete_story")
            or (cached or {}).get("story_text")
        ) if isinstance(cached, dict) else ""
        if (isinstance(cached_parts, list)
                and len(cached_parts) == parts_count
                and clean_text(cached_story) == clean_text(story_text)):
            result = [normalize_text(row.get("narration"))
                      for row in cached_parts if isinstance(row, dict)]
            if len(result) != parts_count:
                raise RuntimeError("Cached final story Part records are invalid.")
        else:
            result = _split_story_into_parts(story_text, parts_count)
    if any(not clean_text(part) for part in result):
        raise RuntimeError("One or more initial Part narrations are empty.")
    reconstructed = clean_text(" ".join(result))
    if reconstructed != clean_text(story_text):
        raise RuntimeError(
            "Initial Part narrations do not reconstruct the complete story."
        )
    return result


def _rebalance_part_units(
    part_texts: list[str],
    minimum: int,
    maximum: int,
) -> tuple[list[list[str]], list[dict]]:
    """Apply the Scene range, carrying complete overflow sentences forward."""
    all_groups: list[list[str]] = []
    carry: list[dict] = []
    distribution: list[dict] = []
    source_sentence_number = 0

    for part_index, original_text in enumerate(part_texts):
        part_number = part_index + 1
        own_sentences = split_sentences(original_text)
        if not own_sentences:
            raise RuntimeError(f"Part {part_number} contains no complete sentences.")
        own_units: list[dict] = []
        for sentence in own_sentences:
            source_sentence_number += 1
            own_units.append({
                "text": sentence,
                "source_sentence": source_sentence_number,
                "source_part": part_number,
                "clause": 0,
            })
        incoming = list(carry)
        units = incoming + own_units
        carry = []
        is_final = part_number == len(part_texts)

        if len(units) > maximum and not is_final:
            assigned = units[:maximum]
            carry = units[maximum:]
            groups = [row["text"] for row in assigned]
        elif len(units) > maximum and is_final:
            expanded_texts = [row["text"] for row in units]
            groups = group_natural_units(expanded_texts, maximum)
            # The final Part cannot carry material beyond the story ending.
            assigned = units
        else:
            if len(units) < minimum:
                units = _expand_rows_to_minimum(units, minimum)
            assigned = units
            texts = [row["text"] for row in units]
            groups = _group_short_related(texts, minimum)
            if len(groups) < minimum:
                groups = texts
            if len(groups) > maximum:
                groups = group_natural_units(groups, maximum)

        if not minimum <= len(groups) <= maximum:
            raise RuntimeError(
                f"Part {part_number} produced {len(groups)} Scenes; configured "
                f"range is {minimum}-{maximum}."
            )

        assigned_text = clean_text(" ".join(row["text"] for row in assigned))
        group_text = clean_text(" ".join(groups))
        if assigned_text != group_text:
            raise RuntimeError(
                f"Part {part_number} Scene groups do not reconstruct its "
                "assigned complete sentences/clauses."
            )

        incoming_ids = [row["source_sentence"] for row in incoming]
        outgoing_ids = [row["source_sentence"] for row in carry]
        all_groups.append(groups)
        distribution.append({
            "part": part_number,
            "original_word_count": word_count(original_text),
            "rebalanced_word_count": word_count(assigned_text),
            "scene_count": len(groups),
            "carried_in_sentence_numbers": incoming_ids,
            "carried_out_sentence_numbers": outgoing_ids,
            "carried_in_count": len(incoming),
            "carried_out_count": len(carry),
            "source_sentence_numbers": [row["source_sentence"] for row in assigned],
        })

    if carry:
        raise RuntimeError("Complete-sentence carry-over remained after final Part.")

    flattened = [group for groups in all_groups for group in groups]
    original_text = clean_text(" ".join(part_texts))
    rebuilt_text = clean_text(" ".join(flattened))
    if original_text != rebuilt_text:
        raise RuntimeError(
            "Scene planning omitted, duplicated, reordered or changed story text."
        )
    return all_groups, distribution


def _make_main_scene(
    format_type: str,
    part_number: int,
    scene_number: int,
    global_number: int,
    total_main_scenes: int,
    anchor: str,
) -> dict:
    beat, description = select_beat(format_type, global_number - 1, total_main_scenes)
    anchor = clean_text(anchor)
    if not anchor:
        raise RuntimeError(f"Main Scene {global_number} has no story anchor.")
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
            "stock scenes, generic imagery or events from another Part."
        ),
    }


def _make_extra_scene(
    part_number: int,
    role: str,
    anchor: str,
    format_type: str,
    final_part: bool,
) -> dict:
    anchor = clean_text(anchor)
    if not anchor:
        raise RuntimeError(f"Part {part_number} {role} record has no anchor.")
    definitions = {
        "hook": {
            "purpose": "Create a story-specific opening that raises curiosity, danger or an important question using established facts.",
            "instruction": "Separate opening scene. Do not replace or renumber main scenes. Do not invent an event, character, clue or outcome.",
        },
        "suspense": {
            "purpose": "Create an unresolved question or consequence from this non-final Part's ending that leads naturally into the next.",
            "instruction": "Separate ending scene for a non-final Part. Do not resolve the central story prematurely or fabricate a cliffhanger.",
        },
        "resolution": {
            "purpose": "Deliver the actual complete-story resolution and close the central conflict using the established ending.",
            "instruction": "Separate final resolution scene. Do not invent a new ending or leave an artificial cliffhanger.",
        },
    }
    if role not in definitions:
        raise RuntimeError(f"Unsupported extra-Scene role: {role}")
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
        "continuity_rule": "Maintain the same established character identities, appearance, clothing, locations, objects and chronology.",
        "invention_policy": "Use only established story facts. Do not add unrelated characters, objects, locations, events, dialogue or twists.",
        "visual_policy": "Depict the exact story-specific role moment with cinematic live-action realism; avoid generic imagery.",
    }


def _build_part_records(
    format_type: str,
    scene_groups: list[list[str]],
    distribution: list[dict],
) -> list[dict]:
    parts: list[dict] = []
    global_number = 0
    total_main_scenes = sum(len(groups) for groups in scene_groups)
    for part_number, anchors in enumerate(scene_groups, 1):
        if not anchors:
            raise RuntimeError(f"Part {part_number} has no Main Scenes.")
        scenes: list[dict] = []
        for scene_number, anchor in enumerate(anchors, 1):
            global_number += 1
            scenes.append(_make_main_scene(
                format_type, part_number, scene_number,
                global_number, total_main_scenes, anchor,
            ))
        first_anchor = anchors[0]
        last_anchor = anchors[-1]
        is_final = part_number == len(scene_groups)
        hook = _make_extra_scene(part_number, "hook", first_anchor, format_type, is_final)
        suspense = None if is_final else _make_extra_scene(
            part_number, "suspense", last_anchor, format_type, False
        )
        resolution = _make_extra_scene(
            part_number, "resolution", last_anchor, format_type, True
        ) if is_final else None

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            expected_id = canonical_main_id(part_number, scene_number)
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid Main Scene {expected_id}.")
            expected_scene_values = {
                "part": part_number,
                "scene": scene_number,
                "global_scene_number": global_number,
                "scene_id": expected_id,
                "role": "main",
            }
            for field, expected in expected_scene_values.items():
                if scene.get(field) != expected:
                    raise RuntimeError(f"Invalid {field} in Main Scene {expected_id}.")
            if expected_id in all_main_ids:
                raise RuntimeError(f"Duplicate Main Scene ID: {expected_id}")
            all_main_ids.add(expected_id)
            for field in (
                "beat", "beat_description", "purpose", "story_anchor",
                "narration", "continuity_rule", "invention_policy", "visual_policy",
            ):
                if not clean_text(scene.get(field)):
                    raise RuntimeError(f"Missing {field} in Main Scene {expected_id}.")
            if clean_text(scene.get("narration")) != clean_text(scene.get("story_anchor")):
                raise RuntimeError(f"Scene narration/anchor mismatch: {expected_id}.")
            part_anchors.append(clean_text(scene.get("story_anchor")))
            all_anchors.append(clean_text(scene.get("story_anchor")))
        if clean_text(" ".join(part_anchors)) != clean_text(part.get("narration")):
            raise RuntimeError(f"Scene narration does not reconstruct Part {part_number}.")
        if clean_text(" ".join(part_anchors)) != clean_text(part.get("story_anchor")):
            raise RuntimeError(f"Part {part_number} anchor differs from ordered Scene anchors.")

        hook = part.get("hook_scene")
        suspense = part.get("suspense_scene")
        resolution = part.get("resolution_scene")
        validate_extra_scene(hook, part_number, "hook", True, is_final)
        validate_extra_scene(suspense, part_number, "suspense", not is_final, False)
        validate_extra_scene(resolution, part_number, "resolution", is_final, True)
        expected_extras = [row for row in (hook, suspense, resolution) if isinstance(row, dict)]
        if part.get("extra_scenes") != expected_extras:
            raise RuntimeError(f"Part {part_number} extra role records are invalid.")
        for extra in expected_extras:
            extra_id = extra["scene_id"]
            if extra_id in all_extra_ids or extra_id in all_main_ids:
                raise RuntimeError(f"Duplicate Scene role ID: {extra_id}")
            all_extra_ids.add(extra_id)
        if clean_text(hook.get("story_anchor")) != part_anchors[0]:
            raise RuntimeError(f"Part {part_number} Hook is not anchored to its opening.")
        if suspense is not None and clean_text(suspense.get("story_anchor")) != part_anchors[-1]:
            raise RuntimeError(f"Part {part_number} Suspense is not anchored to its ending.")
        if resolution is not None and clean_text(resolution.get("story_anchor")) != part_anchors[-1]:
            raise RuntimeError("Final Resolution is not anchored to the final story Scene.")
        total_hooks += 1
        total_suspense += int(suspense is not None)
        total_resolutions += int(resolution is not None)

        dist = distribution[part_number - 1]
        if not isinstance(dist, dict) or dist.get("part") != part_number or dist.get("scene_count") != len(scenes):
            raise RuntimeError(f"Part {part_number} carry-forward distribution is invalid.")
        if dist.get("carried_in_count") != len(dist.get("carried_in_sentence_numbers", [])):
            raise RuntimeError(f"Part {part_number} carried-in count is invalid.")
        if dist.get("carried_out_count") != len(dist.get("carried_out_sentence_numbers", [])):
            raise RuntimeError(f"Part {part_number} carried-out count is invalid.")

    if global_number != total_main:
        raise RuntimeError("Global Main Scene numbering is incorrect.")
    if sum(word_count(anchor) for anchor in all_anchors) != source_words:
        raise RuntimeError("Main Scene anchors do not preserve complete story word count.")
    rebuilt = clean_text(" ".join(all_anchors))
    if sha256_text(rebuilt) != story_hash:
        raise RuntimeError("Main Scene anchors changed the complete story text.")
    expected_counts = {
        "total_hook_scenes": parts_count,
        "total_suspense_scenes": max(0, parts_count - 1),
        "total_resolution_scenes": 1,
        "total_extra_scenes": parts_count + max(0, parts_count - 1) + 1,
    }
    for field, expected in expected_counts.items():
        if plan.get(field) != expected:
            raise RuntimeError(f"Plan metadata mismatch for {field}.")
    if (total_hooks, total_suspense, total_resolutions) != (
        parts_count, max(0, parts_count - 1), 1
    ):
        raise RuntimeError("Hook/Suspense/Resolution role counts are invalid.")
    if distribution[-1].get("carried_out_count") != 0:
        raise RuntimeError("The final Part cannot leave story sentences unassigned.")
    assigned_source_numbers: list[int] = []
    for part_index, row in enumerate(distribution):
        if not isinstance(row, dict):
            raise RuntimeError("Invalid Part carry-forward record.")
        assigned_source_numbers.extend(row.get("source_sentence_numbers", []))
        if part_index + 1 < len(distribution):
            next_incoming = distribution[part_index + 1].get("carried_in_sentence_numbers", [])
            if row.get("carried_out_sentence_numbers", []) != next_incoming:
                raise RuntimeError("Carried sentences are not passed to the next Part in order.")
    if (not assigned_source_numbers
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 1
                   for value in assigned_source_numbers)
            or assigned_source_numbers != sorted(assigned_source_numbers)):
        raise RuntimeError("Source sentence order is invalid after carry-forward.")
    if sorted(set(assigned_source_numbers)) != list(range(1, max(assigned_source_numbers) + 1)):
        raise RuntimeError("One or more source sentences are missing from the Scene plan.")


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
    actual_count = len(parts)
    if not minimum <= actual_count <= maximum:
        raise RuntimeError(
            f"Final story contains {actual_count} Parts; configured range is {minimum}-{maximum}."
        )
    for field in ("selected_parts_count", "selected_parts", "parts_count", "total_parts"):
        declared = story.get(field)
        if declared is not None:
            try:
                declared = int(declared)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid {field} in final story.") from exc
            if declared != actual_count:
                raise RuntimeError(f"Final story {field}={declared} but actual Parts={actual_count}.")
    selected = get_selected_parts(config, required=True)
    if selected is None or int(selected) != actual_count:
        raise RuntimeError("Persisted actual PARTS selection does not match final story.")
    record = get_selected_parts_record(config)
    if not isinstance(record, dict):
        raise RuntimeError("Persisted PARTS selection record is missing.")
    if int(record.get("selected_parts", -1)) != actual_count:
        raise RuntimeError("PARTS selection record does not match final story.")
    if int(record.get("minimum_parts", -1)) != minimum:
        raise RuntimeError("PARTS selection record minimum does not match Input/topic.txt.")
    if int(record.get("maximum_parts", -1)) != maximum:
        raise RuntimeError("PARTS selection record maximum does not match Input/topic.txt.")
    return actual_count


def _validate_final_story_parts(
    story: dict,
    story_text: str,
    parts_count: int,
    scenes_min: int,
    scenes_max: int,
) -> list[str]:
    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Final story Part count is inconsistent.")
    part_texts: list[str] = []
    global_scene = 0
    for index, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Final story Part {index} is invalid.")
        if int(part.get("part", -1)) != index:
            raise RuntimeError(f"Final story Part numbering is invalid at Part {index}.")
        if part.get("part_id") != f"part_{index:02d}":
            raise RuntimeError(f"Final story Part ID mismatch at Part {index}.")
        narration = clean_text(part.get("narration"))
        if not narration:
            raise RuntimeError(f"Final story Part {index} is empty.")
        if int(part.get("word_count", -1)) != word_count(narration):
            raise RuntimeError(f"Final story Part {index} word_count metadata mismatch.")
        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes_min <= len(scenes) <= scenes_max:
            raise RuntimeError(
                f"Final story Part {index} must contain {scenes_min}-{scenes_max} Main Scenes."
            )
        scene_texts: list[str] = []
        for scene_index, scene in enumerate(scenes, 1):
            global_scene += 1
            if not isinstance(scene, dict):
                raise RuntimeError(f"Final story Part {index} Scene {scene_index} is invalid.")
            expected_scene_id = canonical_main_id(index, scene_index)
            if scene.get("scene_id") != expected_scene_id or scene.get("role") != "main":
                raise RuntimeError(f"Final story Scene ID/role mismatch: {expected_scene_id}.")
            if int(scene.get("part", -1)) != index or int(scene.get("scene", -1)) != scene_index:
                raise RuntimeError(f"Final story Scene numbering mismatch: {expected_scene_id}.")
            if int(scene.get("global_scene_number", -1)) != global_scene:
                raise RuntimeError(f"Final story global Scene numbering mismatch: {expected_scene_id}.")
            scene_narration = clean_text(scene.get("narration"))
            if not scene_narration:
                raise RuntimeError(f"{expected_scene_id} has no narration.")
            scene_texts.append(scene_narration)
        if clean_text(" ".join(scene_texts)) != narration:
            raise RuntimeError(f"Scene narration does not reconstruct Part {index}.")
        part_texts.append(narration)
    if clean_text(" ".join(part_texts)) != clean_text(story_text):
        raise RuntimeError("Final Part narrations do not reconstruct the complete corrected story.")
    return part_texts


def get_final_story_data(config: dict) -> tuple[str, int, int, str]:
    """Read final corrected story; return legacy tuple with SCENES minimum."""
    final_story = read_story_from_final_output()
    if final_story is None:
        raise RuntimeError(
            "Completed output/story/story.json is required before planning. "
            "Generate and validate the final corrected story first."
        )
    format_type = clean_text(final_story.get("format")).lower()
    if format_type != get_format(config):
        raise RuntimeError("Final story format does not match Input/topic.txt.")
    minimum_parts, maximum_parts = get_parts_range(config)
    parts_count = _get_actual_part_count(config, final_story, minimum_parts, maximum_parts)
    scenes_min, scenes_max = get_scenes_range(config)
    story_text = normalize_text(
        final_story.get("corrected_complete_story") or final_story.get("story_text")
    )
    if not story_text:
        raise RuntimeError("Final story has no complete corrected story text.")
    if word_count(story_text) < 20:
        raise RuntimeError("Complete final story contains fewer than 20 words.")
    _validate_final_story_parts(final_story, story_text, parts_count, scenes_min, scenes_max)
    title = clean_text(final_story.get("final_title") or final_story.get("title") or get_topic(config))
    if not title:
        raise RuntimeError("Final story title is missing.")
    return story_text, parts_count, scenes_min, title


def main() -> None:
    if not INPUT_FILE.is_file():
        raise RuntimeError(f"Input file is missing: {INPUT_FILE}")
    config = load_and_validate(str(INPUT_FILE))
    format_type = clean_text(get_format(config)).lower()
    topic = clean_text(get_topic(config))
    if format_type not in {"short", "full"}:
        raise RuntimeError(f"Unsupported FORMAT: {format_type!r}.")
    story_text, parts_count, scenes_min, final_title = get_final_story_data(config)
    scenes_range = get_scenes_range(config)
    final_story = read_story_from_final_output()
    if final_story is None:
        raise RuntimeError("Final completed story disappeared before planning.")
    part_texts = [clean_text(part.get("narration")) for part in final_story["parts"]]
    plan = build_plan(
        config=config,
        format_type=format_type,
        topic=topic,
        story_text=story_text,
        parts_count=parts_count,
        scenes_per_part=scenes_range,
        dynamic_short=False,
        part_texts=part_texts,
    )
    plan["final_title"] = final_title
    plan["title_source"] = "final_corrected_story"
    # Re-fingerprint the payload only; title metadata is deliberately outside it.
    old_plan = read_json(OUTPUT_FILE)
    if (
        isinstance(old_plan, dict)
        and old_plan.get("source_fingerprint") == plan.get("source_fingerprint")
        and old_plan.get("script_version") == VERSION
    ):
        try:
            validate_plan(old_plan, format_type, topic, parts_count, scenes_range)
            if old_plan.get("final_title") != final_title:
                raise RuntimeError("Cached plan title is stale.")
            log("Matching final-story variable-scene plan checkpoint preserved.")
            log("External API calls: 0")
            return
        except (RuntimeError, TypeError, ValueError, KeyError) as exc:
            log("Existing plan invalid; rebuilding:", exc)
    write_json(OUTPUT_FILE, plan)
    log("STORY PLAN: SUCCESS")
    log("Version:", VERSION)
    log("Format:", format_type)
    log("Topic:", topic or "(not supplied)")
    log("Final title:", final_title)
    log("Final story words:", word_count(story_text))
    log("Actual selected PARTS:", parts_count)
    log("SCENES range:", f"{scenes_range[0]}-{scenes_range[1]}")
    log("Actual main Scene counts:", ", ".join(map(str, plan["actual_scene_counts"])))
    log("Total main scenes:", plan["total_main_scenes"])
    log("Complete sentences carried forward:", sum(row["carried_out_count"] for row in plan["part_distribution"]))
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