
#!/usr/bin/env python3
"""Build a source-grounded character bible for every story scene and role."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_input_config, normalize_format, get_topic

STORY_DIR = ROOT / "output" / "story"
AI_STORY_FILE = STORY_DIR / "ai_story.json"
SOURCE_STORY_FILE = STORY_DIR / "story.json"
OUTPUT_FILE = STORY_DIR / "character_bible.json"

VERSION = "character-bible-v7-strict-role-continuity"

ROLE_FIELDS = (
    ("hook_scene", "hook"),
    ("suspense_scene", "suspense"),
    ("resolution_scene", "resolution"),
)

DETAIL_FIELDS = (
    "age",
    "gender",
    "appearance",
    "clothing",
    "hair",
    "eyes",
    "face_features",
    "skin_tone",
    "body_features",
    "distinctive_features",
    "personality",
    "role",
    "voice_description",
    "character_description",
    "height",
    "build",
    "accessories",
)

NAME_FIELDS = (
    "character_name",
    "person_name",
    "protagonist",
    "character",
    "name",
)

CAST_FIELDS = (
    "characters",
    "character_list",
    "cast",
    "main_characters",
    "character_bible",
)

DEVANAGARI = r"[\u0900-\u097F]+"

HINDI_NAME_PATTERNS = (
    re.compile(
        rf"(?:नाम\s*(?:था|थी|है)|जिसका नाम)\s*"
        rf"({DEVANAGARI}(?:\s+{DEVANAGARI}){{0,2}})"
    ),
    re.compile(
        rf"(?:श्री|श्रीमती|सुश्री|डॉ\.?|डॉक्टर|मिस्टर|मिस|"
        rf"कप्तान|इंस्पेक्टर|प्रोफेसर)\s+"
        rf"({DEVANAGARI}(?:\s+{DEVANAGARI}){{0,2}})"
    ),
)

ENGLISH_NAME_PATTERNS = (
    re.compile(
        r"\b(?:named|called)\s+"
        r"([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,}){0,2})"
    ),
    re.compile(
        r"\b(?:Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Captain|Detective)\s+"
        r"([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,}){0,2})"
    ),
)

HONORIFICS = {
    "श्री", "श्रीमती", "सुश्री", "डॉ", "डॉक्टर",
    "मिस्टर", "मिस", "कप्तान", "इंस्पेक्टर",
    "Mr", "Mr.", "Mrs", "Mrs.", "Ms", "Ms.",
    "Dr", "Dr.", "Captain", "Detective",
}

STOP_NAMES = {
    "और", "लेकिन", "मगर", "क्योंकि", "इसलिए", "तभी",
    "तब", "जब", "जहाँ", "जहां", "वहाँ", "वहां",
    "यहाँ", "यहां", "उसने", "उसका", "उसकी", "उसके",
    "इसने", "इसका", "इसकी", "इसके", "उन्होंने",
    "उनका", "उनकी", "उनके", "स्वयं", "खुद", "कोई",
    "किसी", "सब", "सभी", "एक", "दो", "तीन",
    "लोग", "लोगों", "व्यक्ति", "आदमी", "औरत",
    "महिला", "पुरुष", "लड़का", "लड़की", "बच्चा",
    "बच्ची", "बच्चे", "बेटा", "बेटी", "पिता",
    "माता", "माँ", "मां", "भाई", "बहन", "पति",
    "पत्नी", "राजा", "रानी", "सैनिक", "पुलिस",
    "अधिकारी", "डॉक्टर", "इंस्पेक्टर", "न्यायाधीश",
    "दिन", "रात", "सुबह", "शाम", "घर", "शहर",
    "गाँव", "गांव", "कमरा", "दरवाजा", "दरवाज़ा",
    "सड़क", "रास्ता", "जंगल", "नदी", "पहाड़",
    "समय", "वक्त", "दुनिया", "कहानी", "घटना",
    "आवाज", "आवाज़", "चेहरा", "आँखें", "आंखें",
    "उस", "इस", "उन", "इन", "वह", "वे", "यह",
    "जो", "जिस", "जिसने", "जिसका", "जिसकी",
    "कौन", "कहाँ", "कहां", "कब", "कैसे",
    "पहले", "बाद", "फिर", "अचानक", "आखिर",
    "अंत", "शुरुआत", "सामने", "पीछे", "पास",
    "दूर", "अंदर", "बाहर", "ऊपर", "नीचे",
}

GENERIC_ENGLISH = {
    "the", "this", "that", "when", "then", "after",
    "before", "scene", "part", "hindi", "india",
    "indian", "story", "meanwhile", "finally",
    "suddenly", "however", "because", "chapter",
    "someone", "nobody", "everyone", "morning",
    "evening", "inside", "outside", "previous",
    "next", "character", "context", "camera",
    "visual", "cinematic", "realistic", "lighting",
}

GENDER_ALIASES = {
    "male": "male",
    "man": "male",
    "boy": "male",
    "पुरुष": "male",
    "आदमी": "male",
    "लड़का": "male",
    "लड़का": "male",
    "female": "female",
    "woman": "female",
    "girl": "female",
    "महिला": "female",
    "औरत": "female",
    "लड़की": "female",
    "लड़की": "female",
    "non-binary": "non_binary",
    "nonbinary": "non_binary",
    "अन्य": "non_binary",
}


def clean(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def log(*values: Any) -> None:
    print(*values, flush=True)


def read_json(path: Path, required: bool = False) -> dict | None:
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")

    return data


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def fingerprint(data: Any) -> str:
    payload = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_name(value: Any) -> str:
    name = clean(value).strip(".,:;!?()[]{}'\"“”‘’।॥-–—")
    tokens = name.split()

    while tokens and tokens[0] in HONORIFICS:
        tokens.pop(0)

    return " ".join(tokens).strip()


def valid_name(value: Any) -> bool:
    name = normalize_name(value)

    if not name or len(name) > 80 or len(name.split()) > 3:
        return False

    if name.casefold() in GENERIC_ENGLISH:
        return False

    if name in STOP_NAMES:
        return False

    if not re.search(r"[A-Za-z\u0900-\u097F]", name):
        return False

    if any(token in STOP_NAMES for token in name.split()):
        return False

    return True


def normalize_names(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, (str, dict)):
        value = [value]

    if not isinstance(value, list):
        return []

    result = []
    seen = set()

    for item in value:
        if isinstance(item, dict):
            name = ""
            for field in NAME_FIELDS:
                name = normalize_name(item.get(field))
                if name:
                    break
        else:
            name = normalize_name(item)

        if not valid_name(name):
            continue

        key = name.casefold()
        if key not in seen:
            seen.add(key)
            result.append(name)

    return result


def explicit_gender(value: Any) -> str:
    return GENDER_ALIASES.get(clean(value).casefold(), "")


def get_story() -> tuple[dict, Path]:
    data = read_json(AI_STORY_FILE, required=True)

    if clean(data.get("status")).lower() not in {
        "completed", "success"
    }:
        raise RuntimeError(
            "ai_story.json must have completed status before "
            "Character Bible generation."
        )

    parts = data.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("ai_story.json contains no story parts.")

    return data, AI_STORY_FILE


def normalize_scene(
    raw: dict,
    part_number: int,
    scene_number: int | None,
    role: str,
    fallback_id: str,
) -> dict:
    scene_id = clean(raw.get("scene_id")) or fallback_id

    if scene_id != fallback_id:
        raise RuntimeError(
            f"Scene ID mismatch: expected {fallback_id}, got {scene_id}."
        )

    declared_role = clean(raw.get("role")).lower()
    if declared_role and declared_role != role:
        raise RuntimeError(
            f"Role mismatch for {scene_id}: expected {role}, "
            f"got {declared_role}."
        )

    narration = clean(
        raw.get("narration")
        or raw.get("text")
        or raw.get("story_text")
        or raw.get("story_anchor")
        or raw.get("beat_description")
        or raw.get("description")
    )

    dialogue = clean(raw.get("dialogue"))
    visual_prompt = clean(
        raw.get("visual_prompt")
        or raw.get("visual_description")
        or raw.get("prompt")
    )

    if not narration and not dialogue and not visual_prompt:
        raise RuntimeError(
            f"Scene {scene_id} has no usable source text."
        )

    names = normalize_names(
        raw.get("characters")
        or raw.get("character_names")
        or raw.get("cast")
    )

    character_ids = []
    raw_ids = raw.get("character_ids", [])

    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]

    if isinstance(raw_ids, list):
        character_ids = [
            clean(value)
            for value in raw_ids
            if clean(value)
        ]

    return {
        "part": part_number,
        "scene": scene_number,
        "scene_id": scene_id,
        "role": role,
        "title": clean(raw.get("title")),
        "narration": narration,
        "dialogue": dialogue,
        "visual_prompt": visual_prompt,
        "location": clean(
            raw.get("location")
            or raw.get("setting")
            or raw.get("world_context")
        ),
        "time_period": clean(raw.get("time_period")),
        "characters": names,
        "character_ids": character_ids,
        "raw": raw,
    }


def collect_scenes(story: dict) -> list[dict]:
    parts = story.get("parts")

    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Story contains no parts.")

    result = []
    seen_ids = set()
    total_parts = len(parts)

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid part {part_number}.")

        if part.get("part") != part_number:
            raise RuntimeError(
                f"Part numbering mismatch at part {part_number}."
            )

        main_scenes = part.get("scenes")

        if not isinstance(main_scenes, list) or not main_scenes:
            raise RuntimeError(
                f"Part {part_number} has no main scenes."
            )

        for scene_number, raw in enumerate(main_scenes, 1):
            if not isinstance(raw, dict):
                raise RuntimeError(
                    f"Invalid main scene in part {part_number}."
                )

            if raw.get("scene") != scene_number:
                raise RuntimeError(
                    f"Main scene numbering mismatch in part {part_number}."
                )

            row = normalize_scene(
                raw,
                part_number,
                scene_number,
                "main",
                f"part_{part_number:02d}_scene_{scene_number:02d}",
            )
            result.append(row)

        for field, role in ROLE_FIELDS:
            raw = part.get(field)

            required = (
                role == "hook"
                or (role == "suspense" and part_number < total_parts)
                or (role == "resolution" and part_number == total_parts)
            )

            forbidden = (
                (role == "suspense" and part_number == total_parts)
                or (role == "resolution" and part_number < total_parts)
            )

            if forbidden and raw is not None:
                raise RuntimeError(
                    f"Part {part_number} cannot contain {role}."
                )

            if required and not isinstance(raw, dict):
                raise RuntimeError(
                    f"Part {part_number} requires exactly one {role} record."
                )

            if raw is None:
                continue

            if not isinstance(raw, dict):
                raise RuntimeError(
                    f"Invalid {role} record in part {part_number}."
                )

            row = normalize_scene(
                raw,
                part_number,
                None,
                role,
                f"part_{part_number:02d}_{role}",
            )
            result.append(row)

        extra_list = part.get("extra_scenes")

        if extra_list is not None:
            if not isinstance(extra_list, list):
                raise RuntimeError(
                    f"extra_scenes must be a list in part {part_number}."
                )

            allowed_ids = {
                row["scene_id"]
                for row in result
                if row["part"] == part_number
            }

            for extra in extra_list:
                if not isinstance(extra, dict):
                    raise RuntimeError("Invalid extra scene record.")

                extra_id = clean(extra.get("scene_id"))

                if extra_id and extra_id not in allowed_ids:
                    raise RuntimeError(
                        f"Unsupported extra scene {extra_id}."
                    )

    for row in result:
        if row["scene_id"] in seen_ids:
            raise RuntimeError(
                f"Duplicate scene ID: {row['scene_id']}."
            )
        seen_ids.add(row["scene_id"])

    expected_roles = {
        "main": sum(len(part["scenes"]) for part in parts),
        "hook": total_parts,
        "suspense": max(0, total_parts - 1),
        "resolution": 1,
    }

    actual_roles = {
        role: sum(row["role"] == role for row in result)
        for role in expected_roles
    }

    if actual_roles != expected_roles:
        raise RuntimeError(
            f"Scene role count mismatch: {actual_roles}; "
            f"expected {expected_roles}."
        )

    return result


def source_text(row: dict) -> str:
    return " ".join(
        value
        for value in (
            clean(row.get("title")),
            clean(row.get("narration")),
            clean(row.get("dialogue")),
        )
        if value
    )


def extract_explicit_names(row: dict) -> list[str]:
    text = source_text(row)
    candidates = set()

    for pattern in HINDI_NAME_PATTERNS:
        for match in pattern.findall(text):
            name = normalize_name(match)
            if valid_name(name):
                candidates.add(name)

    for pattern in ENGLISH_NAME_PATTERNS:
        for match in pattern.findall(text):
            name = normalize_name(match)
            if valid_name(name):
                candidates.add(name)

    raw = row["raw"]

    for field in NAME_FIELDS:
        name = normalize_name(raw.get(field))
        if valid_name(name):
            candidates.add(name)

    for name in row.get("characters", []):
        name = normalize_name(name)
        if valid_name(name):
            candidates.add(name)

    return sorted(candidates, key=str.casefold)


def collect_global_records(story: dict) -> list[dict]:
    records = []

    for key in CAST_FIELDS:
        value = story.get(key)

        if isinstance(value, dict):
            value = value.get("characters", [])

        if isinstance(value, list):
            records.extend(
                item for item in value
                if isinstance(item, dict)
            )

    return records


def new_character(name: str) -> dict:
    return {
        "name": name,
        "mention_count": 0,
        "scene_ids": [],
        "scene_keys": [],
        "aliases": [],
        "detail_values": {
            field: [] for field in DETAIL_FIELDS
        },
        "explicit_gender_values": [],
        "gender_evidence": [],
        "source_character_ids": [],
    }


def add_detail(target: dict, field: str, value: Any) -> None:
    value = clean(value)

    if not value:
        return

    values = target["detail_values"].setdefault(field, [])

    if value not in values:
        values.append(value)


def add_occurrence(
    target: dict,
    row: dict,
    name: str,
) -> None:
    target["mention_count"] += 1

    if row["scene_id"] not in target["scene_ids"]:
        target["scene_ids"].append(row["scene_id"])

    scene_key = {
        "part": row["part"],
        "scene": row["scene"],
        "scene_id": row["scene_id"],
        "role": row["role"],
    }

    if scene_key not in target["scene_keys"]:
        target["scene_keys"].append(scene_key)

    if name.casefold() != target["name"].casefold():
        if name not in target["aliases"]:
            target["aliases"].append(name)

    raw = row["raw"]

    for field in DETAIL_FIELDS:
        add_detail(target, field, raw.get(field))

    declared = explicit_gender(raw.get("gender"))

    if declared:
        target["explicit_gender_values"].append(declared)
        target["gender_evidence"].append({
            "value": declared,
            "source": "explicit scene field",
            "confidence": "explicit",
            "scene_id": row["scene_id"],
        })

    for source_id in row.get("character_ids", []):
        if source_id not in target["source_character_ids"]:
            target["source_character_ids"].append(source_id)


def add_global_character_record(
    found: dict[str, dict],
    raw: dict,
    source_label: str,
) -> None:
    name = ""

    for field in NAME_FIELDS:
        name = normalize_name(raw.get(field))
        if name:
            break

    if not valid_name(name):
        return

    key = name.casefold()

    if key not in found:
        found[key] = new_character(name)

    item = found[key]

    for field in DETAIL_FIELDS:
        add_detail(item, field, raw.get(field))

    declared = explicit_gender(raw.get("gender"))

    if declared:
        item["explicit_gender_values"].append(declared)
        item["gender_evidence"].append({
            "value": declared,
            "source": source_label,
            "confidence": "explicit",
        })

    for alias in normalize_names(raw.get("aliases", [])):
        if (
            alias.casefold() != key
            and alias not in item["aliases"]
        ):
            item["aliases"].append(alias)

    raw_id = clean(
        raw.get("character_id") or raw.get("id")
    )

    if raw_id and raw_id not in item["source_character_ids"]:
        item["source_character_ids"].append(raw_id)


def aggregate_characters(
    scenes: list[dict],
    story: dict,
    supplemental_story: dict | None,
) -> dict[str, dict]:
    found = {}

    for row in scenes:
        for name in extract_explicit_names(row):
            key = name.casefold()

            if key not in found:
                found[key] = new_character(name)

            add_occurrence(found[key], row, name)

    for raw in collect_global_records(story):
        add_global_character_record(
            found,
            raw,
            "explicit AI story cast record",
        )

    if supplemental_story:
        for raw in collect_global_records(supplemental_story):
            add_global_character_record(
                found,
                raw,
                "explicit source story cast record",
            )

    return found


def choose_gender(found: dict) -> tuple[str, str]:
    values = set(found["explicit_gender_values"])

    if len(values) == 1:
        return next(iter(values)), "explicit source evidence"

    if len(values) > 1:
        return "unspecified", "conflicting explicit source evidence"

    return "unspecified", "not explicitly established in source"


def supported_detail(
    values: list[str],
    fallback: str,
) -> str:
    unique = list(dict.fromkeys(
        clean(value) for value in values if clean(value)
    ))

    if not unique:
        return fallback

    if len(unique) == 1:
        return unique[0]

    return (
        "Scene-specific source variants. Apply only the detail "
        "belonging to its matching scene: "
        + " | ".join(unique[:20])
    )


def build_visual_identity(
    found: dict,
    gender: str,
) -> str:
    values = found["detail_values"]

    details = {
        "age": supported_detail(
            values["age"], "Not specified; do not invent."
        ),
        "gender": gender,
        "face": supported_detail(
            values["face_features"],
            "Not specified; preserve source identity without invention.",
        ),
        "appearance": supported_detail(
            values["appearance"], "Not specified; do not invent."
        ),
        "skin tone": supported_detail(
            values["skin_tone"], "Not specified; do not invent."
        ),
        "hair": supported_detail(
            values["hair"], "Not specified; do not invent."
        ),
        "eyes": supported_detail(
            values["eyes"], "Not specified; do not invent."
        ),
        "body": supported_detail(
            values["body_features"],
            "Natural realistic anatomy; no invented traits.",
        ),
        "height": supported_detail(
            values["height"], "Not specified; do not invent."
        ),
        "build": supported_detail(
            values["build"], "Not specified; do not invent."
        ),
        "clothing": supported_detail(
            values["clothing"],
            "Use clothing supported by the matching scene only.",
        ),
        "accessories": supported_detail(
            values["accessories"], "Not specified; do not invent."
        ),
        "distinctive features": supported_detail(
            values["distinctive_features"],
            "Not specified; do not invent.",
        ),
    }

    return (
        f"Character identity: {found['name']}. "
        + " ".join(
            f"{key}: {value}."
            for key, value in details.items()
        )
        + " Maintain the same recognizable face, age range, body "
        "proportions, hair identity and defining features in connected "
        "scenes. Change clothing, expression, pose or condition only "
        "when the matching story scene explicitly supports that change. "
        "Do not change identity between parts or scene roles."
    )


def build_character(
    found: dict,
    index: int,
) -> dict:
    gender, gender_reason = choose_gender(found)
    values = found["detail_values"]
    character_id = f"character_{index:03d}"

    return {
        "id": character_id,
        "character_id": character_id,
        "name": found["name"],
        "aliases": found["aliases"],
        "source_character_ids": found["source_character_ids"],
        "role": supported_detail(
            values["role"], "Follow the actual story role."
        ),
        "importance": (
            "recurring"
            if found["mention_count"] > 1
            else "mentioned"
        ),
        "age": supported_detail(
            values["age"], "Not specified in source."
        ),
        "gender": gender,
        "gender_reason": gender_reason,
        "gender_evidence": found["gender_evidence"][:60],
        "personality": supported_detail(
            values["personality"], "Not specified in source."
        ),
        "appearance": supported_detail(
            values["appearance"], "Not specified in source."
        ),
        "face_features": supported_detail(
            values["face_features"], "Not specified in source."
        ),
        "skin_tone": supported_detail(
            values["skin_tone"], "Not specified in source."
        ),
        "hair": supported_detail(
            values["hair"], "Not specified in source."
        ),
        "eyes": supported_detail(
            values["eyes"], "Not specified in source."
        ),
        "body_features": supported_detail(
            values["body_features"],
            "Natural realistic anatomy; do not invent body details.",
        ),
        "height": supported_detail(
            values["height"], "Not specified in source."
        ),
        "build": supported_detail(
            values["build"], "Not specified in source."
        ),
        "clothing": supported_detail(
            values["clothing"],
            "Use only clothing described for the matching scene.",
        ),
        "accessories": supported_detail(
            values["accessories"], "Not specified in source."
        ),
        "distinctive_features": supported_detail(
            values["distinctive_features"],
            "Not specified in source.",
        ),
        "visual_identity": build_visual_identity(found, gender),
        "continuity_notes": (
            "One stable identity across every part and every scene role. "
            "Hook, Main, Suspense and Resolution use the same character "
            "identity. Scene-specific changes require source support."
        ),
        "consistency_rules": [
            "Keep the same recognizable face and facial structure.",
            "Keep age range, gender identity and body proportions consistent.",
            "Keep hair identity consistent unless the story changes it.",
            "Maintain natural human anatomy, hands, fingers and limbs.",
            "Keep clothing consistent unless the story describes a change.",
            "Do not transfer another character's face or clothing.",
            "Do not invent physical traits missing from the source.",
            "Apply scene-specific appearance only to its matching scene.",
            "Maintain identity in Hook, Main, Suspense and Resolution.",
            "Do not add characters absent from the source scene.",
        ],
        "mention_count": found["mention_count"],
        "scene_ids": found["scene_ids"],
        "scene_keys": found["scene_keys"],
    }


def build_scene_character_map(
    scenes: list[dict],
    characters: list[dict],
) -> list[dict]:
    """Map every Main and separate role scene to stable character IDs."""

    lookup = {}
    character_by_id = {
        item["id"]: item for item in characters
    }

    for character in characters:
        for name in [
            character.get("name", ""),
            *character.get("aliases", []),
        ]:
            normalized = normalize_name(name)

            if normalized:
                lookup[normalized.casefold()] = character["id"]

        for source_id in character.get("source_character_ids", []):
            if source_id:
                lookup[str(source_id).casefold()] = character["id"]

    result = []

    for row in scenes:
        text = source_text(row)
        names = list(row.get("characters", []))
        ids = []

        # Only complete name matches are accepted.
        for character in characters:
            candidates = [
                character.get("name", ""),
                *character.get("aliases", []),
            ]

            for candidate in candidates:
                candidate = normalize_name(candidate)

                if not candidate:
                    continue

                if re.search(
                    re.escape(candidate),
                    text,
                    flags=re.IGNORECASE,
                ):
                    names.append(character["name"])
                    break

        for name in names:
            normalized = normalize_name(name)

            if not normalized:
                continue

            character_id = lookup.get(normalized.casefold())

            if character_id and character_id not in ids:
                ids.append(character_id)

        # Resolve explicitly supplied source IDs only when their
        # corresponding character exists in this Bible.
        for source_id in row.get("character_ids", []):
            character_id = lookup.get(str(source_id).casefold())

            if character_id and character_id not in ids:
                ids.append(character_id)

        canonical_names = [
            character_by_id[item]["name"]
            for item in ids
            if item in character_by_id
        ]

        result.append({
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "role": row["role"],
            "character_ids": ids,
            "character_names": canonical_names,
            "characters": canonical_names,
        })

    return result


def build_scene_world_map(
    scenes: list[dict],
) -> list[dict]:
    result = []

    for row in scenes:
        result.append({
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "role": row["role"],
            "location": row["location"] or "Not specified",
            "time_period": row["time_period"] or "Not specified",
            "continuity_rule": (
                "Use the source location and time period for this scene. "
                "Do not replace it with an unrelated setting."
            ),
        })

    return result


def build_world(scenes: list[dict]) -> dict:
    locations = []
    periods = []

    for row in scenes:
        location = clean(row.get("location"))
        period = clean(row.get("time_period"))

        if location and location not in locations:
            locations.append(location)

        if period and period not in periods:
            periods.append(period)

    return {
        "setting": "; ".join(locations[:50]) or "Not specified",
        "time_period": "; ".join(periods[:30]) or "Not specified",
        "scene_world_map": build_scene_world_map(scenes),
        "geography": "Use only locations supported by the story.",
        "architecture": "Match the source scene and time period.",
        "environment": "Follow scene descriptions and continuity.",
        "weather_style": "Use explicit source details only.",
        "color_and_lighting": (
            "Natural cinematic lighting, realistic skin tones, "
            "moderate contrast and visible shadow detail."
        ),
        "continuity_rules": [
            "Keep recurring locations visually recognizable.",
            "Keep connected locations and time periods consistent.",
            "Avoid anachronistic clothing, technology and architecture.",
            "Do not add locations that contradict the story.",
            "Do not force one weather condition across unrelated scenes.",
            "Use scene-specific environmental details where supplied.",
            "Preserve chronology and location changes from the story.",
        ],
    }


def collect_supplemental_source() -> dict | None:
    data = read_json(SOURCE_STORY_FILE)

    if not isinstance(data, dict):
        return None

    status = clean(data.get("status")).lower()

    if status and status not in {
        "completed", "planned", "in_progress", "success"
    }:
        return None

    return data


def parse_enabled(
    value: Any,
    default: bool = True,
) -> bool:
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    return clean(value).lower() in {
        "true", "1", "yes", "on", "enabled"
    }


def validate_bible(
    bible: dict,
    scenes: list[dict],
) -> None:
    if bible.get("status") not in {"completed", "disabled"}:
        raise RuntimeError("Character Bible has an invalid status.")

    characters = bible.get("characters")
    scene_map = bible.get("scene_character_map")

    if not isinstance(characters, list):
        raise RuntimeError("characters must be a list.")

    if not isinstance(scene_map, list):
        raise RuntimeError("scene_character_map must be a list.")

    if bible["status"] == "disabled":
        return

    character_ids = set()
    character_names = set()

    for item in characters:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid character record.")

        character_id = clean(item.get("id"))
        name = clean(item.get("name"))

        if not character_id or not name:
            raise RuntimeError(
                "Every character requires a stable ID and name."
            )

        if character_id in character_ids:
            raise RuntimeError(
                f"Duplicate character ID: {character_id}"
            )

        if name.casefold() in character_names:
            raise RuntimeError(
                f"Duplicate character name: {name}"
            )

        character_ids.add(character_id)
        character_names.add(name.casefold())

        if item.get("character_id") != character_id:
            raise RuntimeError(
                f"character_id mismatch for {name}."
            )

        if not clean(item.get("visual_identity")):
            raise RuntimeError(
                f"Missing visual_identity for {name}."
            )

        rules = item.get("consistency_rules")

        if not isinstance(rules, list) or not rules:
            raise RuntimeError(
                f"Missing consistency rules for {name}."
            )

        if not isinstance(item.get("scene_ids"), list):
            raise RuntimeError(
                f"Invalid scene_ids for {name}."
            )

    expected = {
        (
            row["part"],
            row["scene"],
            row["scene_id"],
            row["role"],
        )
        for row in scenes
    }

    actual = set()

    for row in scene_map:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid scene-character map row.")

        key = (
            row.get("part"),
            row.get("scene"),
            row.get("scene_id"),
            row.get("role"),
        )

        if key in actual:
            raise RuntimeError(
                f"Duplicate scene-character mapping: {key}"
            )

        actual.add(key)

        ids = row.get("character_ids", [])

        if not isinstance(ids, list):
            raise RuntimeError(
                f"Invalid character_ids for scene {key}."
            )

        unknown = [
            value for value in ids
            if value not in character_ids
        ]

        if unknown:
            raise RuntimeError(
                f"Unknown character IDs in scene {key}: {unknown}"
            )

        names = row.get("character_names", [])

        if not isinstance(names, list):
            raise RuntimeError(
                f"Invalid character_names for scene {key}."
            )

        expected_names = [
            next(
                item["name"]
                for item in characters
                if item["id"] == character_id
            )
            for character_id in ids
        ]

        if names != expected_names:
            raise RuntimeError(
                f"Character names/IDs mismatch in scene {key}."
            )

    if actual != expected:
        missing = expected - actual
        extra = actual - expected

        raise RuntimeError(
            "Scene-character mapping mismatch. "
            f"Missing={len(missing)}, extra={len(extra)}."
        )

    if bible.get("character_count") != len(characters):
        raise RuntimeError("Character count metadata mismatch.")

    if bible.get("scene_count") != len(scenes):
        raise RuntimeError("Scene count metadata mismatch.")

    main_count = sum(
        row["role"] == "main" for row in scenes
    )
    extra_count = len(scenes) - main_count

    if bible.get("main_scene_count") != main_count:
        raise RuntimeError("Main scene count metadata mismatch.")

    if bible.get("extra_scene_count") != extra_count:
        raise RuntimeError("Extra scene count metadata mismatch.")

    if not isinstance(bible.get("world"), dict):
        raise RuntimeError("World continuity data is missing.")


def build_disabled_bible(
    config: dict,
    story: dict,
    source_path: Path,
    scenes: list[dict],
) -> dict:
    return {
        "status": "disabled",
        "version": VERSION,
        "generation_mode": "disabled_by_input",
        "api_calls": 0,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "source_fingerprint": fingerprint(story),
        "topic": clean(
            get_topic(config)
            or story.get("topic")
            or story.get("title")
        ),
        "title": clean(story.get("title")),
        "format": normalize_format(config),
        "character_count": 0,
        "scene_count": len(scenes),
        "main_scene_count": sum(
            row["role"] == "main" for row in scenes
        ),
        "extra_scene_count": sum(
            row["role"] != "main" for row in scenes
        ),
        "characters": [],
        "scene_character_map": [],
        "world": {},
        "warnings": [
            "CHARACTER_BIBLE is disabled in Input/topic.txt."
        ],
    }


def build_bible(
    config: dict,
    story: dict,
    source_path: Path,
    scenes: list[dict],
    supplemental_story: dict | None = None,
) -> dict:
    topic = clean(
        get_topic(config)
        or story.get("topic")
        or story.get("title")
    )

    found = aggregate_characters(
        scenes,
        story,
        supplemental_story,
    )

    ordered = sorted(
        found.values(),
        key=lambda item: (
            -item["mention_count"],
            item["name"].casefold(),
        ),
    )

    characters = [
        build_character(item, index)
        for index, item in enumerate(ordered, 1)
    ]

    scene_map = build_scene_character_map(
        scenes,
        characters,
    )

    world = build_world(scenes)

    fingerprint_data = {
        "version": VERSION,
        "story": story,
        "supplemental_story": supplemental_story or {},
        "format": normalize_format(config),
        "character_consistency": config.get(
            "CHARACTER_CONSISTENCY", True
        ),
        "world_consistency": config.get(
            "WORLD_CONSISTENCY", True
        ),
        "scene_roles": [
            {
                "scene_id": row["scene_id"],
                "part": row["part"],
                "scene": row["scene"],
                "role": row["role"],
                "narration": row["narration"],
                "dialogue": row["dialogue"],
                "visual_prompt": row["visual_prompt"],
                "characters": row["characters"],
                "character_ids": row["character_ids"],
                "location": row["location"],
                "time_period": row["time_period"],
            }
            for row in scenes
        ],
    }

    warnings = []

    if not characters:
        warnings.append(
            "No reliable character names or explicit cast records "
            "were found. No fictional identities were invented."
        )

    if any(
        item["gender"] == "unspecified"
        for item in characters
    ):
        warnings.append(
            "Some character genders remain unspecified because "
            "explicit source evidence was unavailable or conflicting."
        )

    if any(
        not row["characters"]
        for row in scenes
    ):
        warnings.append(
            "Some scenes have no explicitly identifiable character. "
            "No character was assigned by guesswork."
        )

    bible = {
        "status": "completed",
        "version": VERSION,
        "generation_mode": "offline_source_grounded_all_scene_roles",
        "api_calls": 0,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "source_fingerprint": fingerprint(fingerprint_data),
        "topic": topic,
        "title": clean(story.get("title") or topic),
        "format": normalize_format(config),
        "character_count": len(characters),
        "scene_count": len(scenes),
        "main_scene_count": sum(
            row["role"] == "main" for row in scenes
        ),
        "extra_scene_count": sum(
            row["role"] != "main" for row in scenes
        ),
        "characters": characters,
        "scene_character_map": scene_map,
        "world": world,
        "warnings": warnings,
        "limitations": [
            "No external AI calls are made in this stage.",
            "Names are taken from explicit scene fields, clear naming "
            "phrases and declared cast records.",
            "Main, Hook, Suspense and Resolution scenes are all examined.",
            "Visual prompts are not treated as proof of character identity.",
            "Gender is assigned only from explicit source fields.",
            "Unspecified physical traits are not fabricated.",
            "Scene-specific clothing changes remain limited to their source scene.",
            "All scene roles use stable character IDs and identity rules.",
            "Unknown source character IDs are not converted into invented people.",
        ],
    }

    validate_bible(bible, scenes)
    return bible


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — CHARACTER BIBLE")
    log("=" * 64)

    input_path = ROOT / "Input" / "topic.txt"

    if not input_path.is_file():
        raise RuntimeError(
            f"Input file is missing: {input_path}"
        )

    config = load_input_config(input_path)
    story, source_path = get_story()
    scenes = collect_scenes(story)
    supplemental_story = collect_supplemental_source()

    enabled = parse_enabled(
        config.get("CHARACTER_BIBLE"),
        default=True,
    )

    if not enabled:
        bible = build_disabled_bible(
            config,
            story,
            source_path,
            scenes,
        )
        write_json(OUTPUT_FILE, bible)

        log("Character Bible disabled by Input/topic.txt.")
        log("Output:", OUTPUT_FILE.relative_to(ROOT))
        log("API calls: 0")
        return 0

    bible = build_bible(
        config=config,
        story=story,
        source_path=source_path,
        scenes=scenes,
        supplemental_story=supplemental_story,
    )

    existing = read_json(OUTPUT_FILE)

    if (
        isinstance(existing, dict)
        and existing.get("version") == VERSION
        and existing.get("source_fingerprint")
        == bible.get("source_fingerprint")
    ):
        try:
            validate_bible(existing, scenes)

            log("Matching valid Character Bible checkpoint preserved.")
            log("Characters:", existing["character_count"])
            log("Scene mappings:", len(existing["scene_character_map"]))
            log("API calls: 0")
            return 0

        except (RuntimeError, TypeError, ValueError, KeyError) as exc:
            log(
                "Existing Character Bible is invalid; rebuilding:",
                exc,
            )

    write_json(OUTPUT_FILE, bible)

    log("Character Bible created.")
    log("Version:", VERSION)
    log("Source:", source_path.relative_to(ROOT))
    log("Scenes examined:", len(scenes))
    log("Main scenes:", bible["main_scene_count"])
    log("Extra scenes:", bible["extra_scene_count"])
    log("Characters detected:", bible["character_count"])
    log("Scene mappings:", len(bible["scene_character_map"]))
    log("API calls:", bible["api_calls"])
    log("Output:", OUTPUT_FILE.relative_to(ROOT))

    for warning in bible["warnings"]:
        log("WARNING:", warning)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Character Bible generation interrupted.")
        sys.exit(130)
    except Exception as exc:
        log(
            "CHARACTER BIBLE FAILED:",
            f"{type(exc).__name__}: {exc}",
        )
        raise SystemExit(1)
