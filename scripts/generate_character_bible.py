#!/usr/bin/env python3
"""Create a source-grounded character bible with gender evidence and scene continuity."""

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

VERSION = "character-bible-v8-evidence-physical-continuity-all-roles"

ROLE_FIELDS = (
    ("hook_scene", "hook"),
    ("suspense_scene", "suspense"),
    ("resolution_scene", "resolution"),
)

DETAIL_FIELDS = (
    "age", "gender", "appearance", "clothing", "hair", "eyes",
    "face_features", "skin_tone", "body_features", "distinctive_features",
    "personality", "role", "voice_description", "character_description",
    "height", "build", "accessories",
)

PHYSICAL_FIELDS = (
    "age", "gender", "appearance", "face_features", "skin_tone",
    "hair", "eyes", "body_features", "height", "build",
    "distinctive_features",
)

NAME_FIELDS = (
    "character_name", "person_name", "protagonist", "character", "name"
)

CAST_FIELDS = (
    "characters", "character_list", "cast", "main_characters",
    "character_bible"
)

DEVANAGARI = r"[\u0900-\u097F]+"

HINDI_NAME_PATTERNS = (
    re.compile(
        rf"(?:नाम\s*(?:था|थी|है)|जिसका नाम|जिसका नामकरण)\s*"
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
    "श्री", "श्रीमती", "सुश्री", "डॉ", "डॉक्टर", "मिस्टर",
    "मिस", "कप्तान", "इंस्पेक्टर", "Mr", "Mr.", "Mrs",
    "Mrs.", "Ms", "Ms.", "Dr", "Dr.", "Captain", "Detective",
}

STOP_NAMES = {
    "और", "लेकिन", "मगर", "क्योंकि", "इसलिए", "तभी", "तब",
    "जब", "जहाँ", "जहां", "वहाँ", "वहां", "यहाँ", "यहां",
    "उसने", "उसका", "उसकी", "उसके", "इसने", "इसका", "इसकी",
    "इसके", "उन्होंने", "उनका", "उनकी", "उनके", "स्वयं",
    "खुद", "कोई", "किसी", "सब", "सभी", "एक", "दो", "तीन",
    "लोग", "लोगों", "व्यक्ति", "आदमी", "औरत", "महिला",
    "पुरुष", "लड़का", "लड़की", "बच्चा", "बच्ची", "बच्चे",
    "बेटा", "बेटी", "पिता", "माता", "माँ", "मां", "भाई",
    "बहन", "पति", "पत्नी", "राजा", "रानी", "सैनिक",
    "पुलिस", "अधिकारी", "डॉक्टर", "इंस्पेक्टर", "न्यायाधीश",
    "दिन", "रात", "सुबह", "शाम", "घर", "शहर", "गाँव",
    "गांव", "कमरा", "दरवाजा", "दरवाज़ा", "सड़क", "रास्ता",
    "जंगल", "नदी", "पहाड़", "समय", "वक्त", "दुनिया",
    "कहानी", "घटना", "आवाज", "आवाज़", "चेहरा", "आँखें",
    "आंखें", "उस", "इस", "उन", "इन", "वह", "वे", "यह",
    "जो", "जिस", "जिसने", "जिसका", "जिसकी", "कौन",
    "कहाँ", "कहां", "कब", "कैसे", "पहले", "बाद", "फिर",
    "अचानक", "आखिर", "अंत", "शुरुआत", "सामने", "पीछे",
    "पास", "दूर", "अंदर", "बाहर", "ऊपर", "नीचे",
}

GENERIC_ENGLISH = {
    "the", "this", "that", "when", "then", "after", "before",
    "scene", "part", "hindi", "india", "indian", "story",
    "meanwhile", "finally", "suddenly", "however", "because",
    "chapter", "someone", "nobody", "everyone", "morning",
    "evening", "inside", "outside", "previous", "next",
    "character", "context", "camera", "visual", "cinematic",
    "realistic", "lighting",
}

GENDER_ALIASES = {
    "male": "male", "man": "male", "boy": "male",
    "पुरुष": "male", "आदमी": "male", "लड़का": "male",
    "लड़का": "male", "पुत्र": "male", "बेटा": "male",
    "father": "male", "son": "male", "he": "male",
    "him": "male", "his": "male",
    "female": "female", "woman": "female", "girl": "female",
    "महिला": "female", "औरत": "female", "लड़की": "female",
    "लड़की": "female", "पुत्री": "female", "बेटी": "female",
    "mother": "female", "daughter": "female", "she": "female",
    "her": "female", "hers": "female",
    "non-binary": "non_binary", "nonbinary": "non_binary",
    "अन्य": "non_binary",
}

HINDI_MALE_MARKERS = (
    r"\b(?:वह|वे|उसने|उसका|उसकी|उसके|उन्होंने|उनका|उनकी|"
    r"उनके)\b.{0,55}\b(?:गया|आया|बैठा|उठा|बोला|कहा|करता|"
    r"रहा|था|हुआ|चुका|सकता)\b",
    r"\b(?:गया|आया|बैठा|उठा|बोला|कहा|करता|रहा|था|हुआ|"
    r"चुका|सकता)\b",
)

HINDI_FEMALE_MARKERS = (
    r"\b(?:वह|वे|उसने|उसका|उसकी|उसके|उन्होंने|उनका|उनकी|"
    r"उनके)\b.{0,55}\b(?:गई|आई|बैठी|उठी|बोली|कही|करती|"
    r"रही|थी|हुई|चुकी|सकती)\b",
    r"\b(?:गई|आई|बैठी|उठी|बोली|कही|करती|रही|थी|हुई|"
    r"चुकी|सकती)\b",
)

ENGLISH_MALE_MARKERS = re.compile(
    r"\b(?:he|him|his|himself|man|boy|father|son|husband)\b",
    re.IGNORECASE,
)
ENGLISH_FEMALE_MARKERS = re.compile(
    r"\b(?:she|her|hers|herself|woman|girl|mother|daughter|wife)\b",
    re.IGNORECASE,
)


def clean(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def log(*values: Any) -> None:
    print(*values, flush=True)


def fingerprint(data: Any) -> str:
    payload = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    if name.casefold() in GENERIC_ENGLISH or name in STOP_NAMES:
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

    result, seen = [], set()
    for item in value:
        if isinstance(item, dict):
            name = ""
            for field in NAME_FIELDS:
                name = normalize_name(item.get(field))
                if name:
                    break
        else:
            name = normalize_name(item)

        if valid_name(name) and name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
    return result


def explicit_gender(value: Any) -> str:
    return GENDER_ALIASES.get(clean(value).casefold(), "")


def get_story() -> tuple[dict, Path]:
    data = read_json(AI_STORY_FILE, required=True)
    if clean(data.get("status")).lower() not in {"completed", "success"}:
        raise RuntimeError(
            "ai_story.json must have completed status before Character Bible generation."
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
    scene_id = clean(raw.get("scene_id"))
    if scene_id != fallback_id:
        raise RuntimeError(
            f"Scene ID mismatch: expected {fallback_id}, got {scene_id or 'missing'}."
        )

    declared_role = clean(raw.get("role")).lower()
    if declared_role != role:
        raise RuntimeError(
            f"Role mismatch for {scene_id}: expected {role}, got {declared_role or 'missing'}."
        )

    narration = clean(
        raw.get("narration") or raw.get("text")
        or raw.get("story_text") or raw.get("story_anchor")
        or raw.get("beat_description") or raw.get("description")
    )
    dialogue = clean(raw.get("dialogue"))
    visual_prompt = clean(
        raw.get("visual_prompt") or raw.get("visual_description")
        or raw.get("prompt")
    )

    if not narration:
        raise RuntimeError(f"Scene {scene_id} has no narration.")
    if not visual_prompt:
        raise RuntimeError(f"Scene {scene_id} has no visual_prompt.")

    raw_names = (
        raw.get("characters")
        or raw.get("character_names")
        or raw.get("cast")
    )
    names = normalize_names(raw_names)

    raw_ids = raw.get("character_ids", [])
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    if not isinstance(raw_ids, list):
        raise RuntimeError(f"{scene_id}: character_ids must be a list.")
    ids = [clean(value) for value in raw_ids if clean(value)]

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
            raw.get("location") or raw.get("setting")
            or raw.get("world_context")
        ),
        "time_period": clean(raw.get("time_period")),
        "characters": names,
        "character_ids": ids,
        "raw": raw,
    }


def collect_scenes(story: dict) -> list[dict]:
    parts = story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Story contains no parts.")

    result, seen_ids = [], set()
    total_parts = len(parts)

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != part_number:
            raise RuntimeError(f"Invalid part numbering at part {part_number}.")

        main_scenes = part.get("scenes")
        if not isinstance(main_scenes, list) or not main_scenes:
            raise RuntimeError(f"Part {part_number} has no main scenes.")

        for scene_number, raw in enumerate(main_scenes, 1):
            if not isinstance(raw, dict) or raw.get("scene") != scene_number:
                raise RuntimeError(
                    f"Invalid main scene numbering in part {part_number}."
                )
            result.append(normalize_scene(
                raw, part_number, scene_number, "main",
                f"part_{part_number:02d}_scene_{scene_number:02d}",
            ))

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

            result.append(normalize_scene(
                raw, part_number, None, role,
                f"part_{part_number:02d}_{role}",
            ))

        extras = part.get("extra_scenes")
        if extras is not None:
            if not isinstance(extras, list):
                raise RuntimeError(
                    f"extra_scenes must be a list in part {part_number}."
                )
            allowed = {
                row["scene_id"] for row in result
                if row["part"] == part_number
            }
            for extra in extras:
                if not isinstance(extra, dict):
                    raise RuntimeError("Invalid extra scene record.")
                extra_id = clean(extra.get("scene_id"))
                if extra_id not in allowed:
                    raise RuntimeError(
                        f"Unexpected extra scene record: {extra_id or 'missing ID'}."
                    )

    for row in result:
        if row["scene_id"] in seen_ids:
            raise RuntimeError(f"Duplicate scene ID: {row['scene_id']}.")
        seen_ids.add(row["scene_id"])

    expected = {
        "main": sum(len(part["scenes"]) for part in parts),
        "hook": total_parts,
        "suspense": total_parts - 1,
        "resolution": 1,
    }
    actual = {
        role: sum(row["role"] == role for row in result)
        for role in expected
    }
    if actual != expected:
        raise RuntimeError(
            f"Role count mismatch: actual={actual}; expected={expected}."
        )

    return result


def source_text(row: dict) -> str:
    return " ".join(
        value for value in (
            clean(row.get("title")),
            clean(row.get("narration")),
            clean(row.get("dialogue")),
        ) if value
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
        if valid_name(name):
            candidates.add(normalize_name(name))

    return sorted(candidates, key=str.casefold)


def collect_global_records(story: dict) -> list[dict]:
    records = []
    for key in CAST_FIELDS:
        value = story.get(key)
        if isinstance(value, dict):
            value = value.get("characters", [])
        if isinstance(value, list):
            records.extend(
                item for item in value if isinstance(item, dict)
            )
    return records


def new_character(name: str) -> dict:
    return {
        "name": name,
        "mention_count": 0,
        "scene_ids": [],
        "scene_keys": [],
        "aliases": [],
        "detail_values": {field: [] for field in DETAIL_FIELDS},
        "scene_details": [],
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


def gender_evidence_for_name(name: str, text: str, scene_id: str) -> list[dict]:
    """Use nearby Hindi grammar and English pronouns as evidence, never as certainty."""
    evidence = []
    sentences = re.split(r"(?<=[।.!?])\s+|[\n]+", text)

    name_pattern = re.compile(
        r"(?<![\w])" + re.escape(name) + r"(?![\w])",
        re.IGNORECASE,
    )

    for sentence in sentences:
        if not name_pattern.search(sentence):
            continue

        hindi_male = any(
            re.search(pattern, sentence)
            for pattern in HINDI_MALE_MARKERS
        )
        hindi_female = any(
            re.search(pattern, sentence)
            for pattern in HINDI_FEMALE_MARKERS
        )
        english_male = bool(ENGLISH_MALE_MARKERS.search(sentence))
        english_female = bool(ENGLISH_FEMALE_MARKERS.search(sentence))

        if hindi_male and not hindi_female:
            evidence.append({
                "value": "male",
                "source": "Hindi grammatical agreement near named character",
                "confidence": "contextual",
                "scene_id": scene_id,
                "evidence_text": sentence[:240],
            })
        elif hindi_female and not hindi_male:
            evidence.append({
                "value": "female",
                "source": "Hindi grammatical agreement near named character",
                "confidence": "contextual",
                "scene_id": scene_id,
                "evidence_text": sentence[:240],
            })

        if english_male and not english_female:
            evidence.append({
                "value": "male",
                "source": "English pronoun or gendered noun near named character",
                "confidence": "contextual",
                "scene_id": scene_id,
                "evidence_text": sentence[:240],
            })
        elif english_female and not english_male:
            evidence.append({
                "value": "female",
                "source": "English pronoun or gendered noun near named character",
                "confidence": "contextual",
                "scene_id": scene_id,
                "evidence_text": sentence[:240],
            })

    return evidence


def add_occurrence(target: dict, row: dict, name: str) -> None:
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
    scene_detail = {
        "scene_id": row["scene_id"],
        "part": row["part"],
        "scene": row["scene"],
        "role": row["role"],
        "details": {},
    }

    for field in DETAIL_FIELDS:
        value = clean(raw.get(field))
        if value:
            add_detail(target, field, value)
            scene_detail["details"][field] = value

    if scene_detail["details"]:
        target["scene_details"].append(scene_detail)

    declared = explicit_gender(raw.get("gender"))
    if declared:
        target["explicit_gender_values"].append(declared)
        target["gender_evidence"].append({
            "value": declared,
            "source": "explicit scene field",
            "confidence": "explicit",
            "scene_id": row["scene_id"],
        })

    target["gender_evidence"].extend(
        gender_evidence_for_name(
            target["name"], source_text(row), row["scene_id"]
        )
    )

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
        if alias.casefold() != key and alias not in item["aliases"]:
            item["aliases"].append(alias)

    raw_id = clean(raw.get("character_id") or raw.get("id"))
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
            found, raw, "explicit AI story cast record"
        )

    if supplemental_story:
        for raw in collect_global_records(supplemental_story):
            add_global_character_record(
                found, raw, "explicit source story cast record"
            )

    return found


def choose_gender(found: dict) -> tuple[str, str]:
    explicit = set(found["explicit_gender_values"])
    contextual = {
        item["value"]
        for item in found["gender_evidence"]
        if item.get("confidence") == "contextual"
        and item.get("value") in {"male", "female"}
    }

    if len(explicit) == 1:
        return next(iter(explicit)), "explicit source evidence"
    if len(explicit) > 1:
        return "unspecified", "conflicting explicit source evidence"

    if len(contextual) == 1:
        return (
            next(iter(contextual)),
            "consistent Hindi/English grammatical evidence",
        )
    if len(contextual) > 1:
        return "unspecified", "conflicting Hindi/English gender evidence"

    return "unspecified", "not established by source evidence"


def supported_detail(values: list[str], fallback: str) -> str:
    unique = list(dict.fromkeys(
        clean(value) for value in values if clean(value)
    ))
    if not unique:
        return fallback
    if len(unique) == 1:
        return unique[0]
    return (
        "Scene-specific source variants; apply only to the matching scene: "
        + " | ".join(unique[:30])
    )


def build_visual_identity(found: dict, gender: str) -> str:
    values = found["detail_values"]
    labels = {
        "age": "age",
        "gender": "gender",
        "face_features": "face",
        "appearance": "appearance",
        "skin_tone": "skin tone",
        "hair": "hair",
        "eyes": "eyes",
        "body_features": "body",
        "height": "height",
        "build": "build",
        "distinctive_features": "distinctive features",
    }
    defaults = {
        "age": "Not specified; do not invent.",
        "face_features": "Not specified; preserve source identity.",
        "appearance": "Not specified; do not invent.",
        "skin_tone": "Not specified; do not invent.",
        "hair": "Not specified; do not invent.",
        "eyes": "Not specified; do not invent.",
        "body_features": "Natural realistic anatomy; no invented traits.",
        "height": "Not specified; do not invent.",
        "build": "Not specified; do not invent.",
        "distinctive_features": "Not specified; do not invent.",
    }

    details = []
    for field, label in labels.items():
        value = (
            gender if field == "gender"
            else supported_detail(values[field], defaults[field])
        )
        details.append(f"{label}: {value}")

    return (
        f"Stable identity for {found['name']}. "
        + "; ".join(details)
        + ". Preserve recognizable face, age range, gender evidence, "
        "skin tone, hair, eyes, body proportions and distinctive traits "
        "across all parts and roles. Change physical condition only when "
        "the matching story scene explicitly requires it."
    )


def build_character(found: dict, index: int) -> dict:
    gender, gender_reason = choose_gender(found)
    values = found["detail_values"]
    character_id = f"character_{index:03d}"

    physical_constraints = [
        "Do not alter facial identity or merge this character with another.",
        "Keep age range and body proportions stable across all scenes.",
        "Keep gender only as supported by recorded source evidence.",
        "Preserve skin tone, hair, eyes and distinctive facial features.",
        "Use natural human anatomy, realistic hands, fingers and limbs.",
        "Do not invent height, build, scars, marks or other physical traits.",
        "Clothing and accessories may change only when the matching scene supports it.",
        "Apply injuries, aging, disguise or physical changes only where story events require them.",
        "Never transfer another character's physical identity or clothing.",
    ]

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
            "recurring" if found["mention_count"] > 1 else "mentioned"
        ),
        "age": supported_detail(values["age"], "Not specified in source."),
        "gender": gender,
        "gender_reason": gender_reason,
        "gender_evidence": found["gender_evidence"][:100],
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
            values["distinctive_features"], "Not specified in source."
        ),
        "visual_identity": build_visual_identity(found, gender),
        "physical_constraints": physical_constraints,
        "scene_details": found["scene_details"],
        "continuity_notes": (
            "One stable identity across Hook, Main, Suspense and Resolution. "
            "Scene-specific changes must be supported by the matching source."
        ),
        "consistency_rules": physical_constraints + [
            "Maintain identity across every part and scene role.",
            "Do not add a character to a scene without source evidence.",
            "Keep scene-specific clothing and physical state attached to its scene ID.",
        ],
        "mention_count": found["mention_count"],
        "scene_ids": found["scene_ids"],
        "scene_keys": found["scene_keys"],
    }

def build_scene_character_map(
    scenes: list[dict],
    characters: list[dict],
) -> list[dict]:
    name_lookup = {}
    source_id_lookup = {}
    character_by_id = {
        item["id"]: item for item in characters
    }

    for character in characters:
        character_id = character["id"]

        for name in [
            character.get("name", ""),
            *character.get("aliases", []),
        ]:
            normalized = normalize_name(name)
            if normalized:
                key = normalized.casefold()
                previous = name_lookup.get(key)
                if previous and previous != character_id:
                    raise RuntimeError(
                        f"Ambiguous character name or alias: {normalized}"
                    )
                name_lookup[key] = character_id

        for source_id in character.get("source_character_ids", []):
            key = str(source_id).casefold()
            previous = source_id_lookup.get(key)
            if previous and previous != character_id:
                raise RuntimeError(
                    f"Ambiguous source character ID: {source_id}"
                )
            source_id_lookup[key] = character_id

    result = []

    for row in scenes:
        scene_id = row["scene_id"]
        text = source_text(row)
        references = list(row.get("characters", []))
        resolved_ids = []

        # Resolve exact explicit names and aliases.
        for name in references:
            character_id = name_lookup.get(
                normalize_name(name).casefold()
            )
            if character_id and character_id not in resolved_ids:
                resolved_ids.append(character_id)

        # Match complete character names in this scene's source text.
        for character in characters:
            candidates = [
                character.get("name", ""),
                *character.get("aliases", []),
            ]
            for candidate in candidates:
                candidate = normalize_name(candidate)
                if not candidate:
                    continue

                pattern = (
                    r"(?<![\w])"
                    + re.escape(candidate)
                    + r"(?![\w])"
                )
                if re.search(pattern, text, flags=re.IGNORECASE):
                    if character["id"] not in resolved_ids:
                        resolved_ids.append(character["id"])
                    break

        # Resolve declared source IDs only against known cast records.
        for source_id in row.get("character_ids", []):
            character_id = source_id_lookup.get(str(source_id).casefold())
            if not character_id:
                raise RuntimeError(
                    f"{scene_id}: source character ID {source_id!r} "
                    "has no matching Character Bible record."
                )
            if character_id not in resolved_ids:
                resolved_ids.append(character_id)

        canonical_names = [
            character_by_id[character_id]["name"]
            for character_id in resolved_ids
        ]

        result.append({
            "scene_id": scene_id,
            "part": row["part"],
            "scene": row["scene"],
            "role": row["role"],
            "character_ids": resolved_ids,
            "character_names": canonical_names,
            "characters": canonical_names,
        })

    return result


def build_scene_roles(scene_map: list[dict]) -> list[dict]:
    return [
        {
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "role": row["role"],
            "character_ids": list(row["character_ids"]),
            "character_names": list(row["character_names"]),
            "characters": list(row["character_names"]),
        }
        for row in scene_map
    ]


def build_scene_world_map(scenes: list[dict]) -> list[dict]:
    return [
        {
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "role": row["role"],
            "location": row["location"] or "Not specified",
            "time_period": row["time_period"] or "Not specified",
            "continuity_rule": (
                "Use only the source location and time period for this scene. "
                "Do not replace them with unrelated settings."
            ),
        }
        for row in scenes
    ]


def build_world(scenes: list[dict]) -> dict:
    locations, periods = [], []

    for row in scenes:
        location = clean(row.get("location"))
        period = clean(row.get("time_period"))

        if location and location not in locations:
            locations.append(location)
        if period and period not in periods:
            periods.append(period)

    return {
        "setting": "; ".join(locations[:100]) or "Not specified",
        "time_period": "; ".join(periods[:50]) or "Not specified",
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


def parse_enabled(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return clean(value).lower() in {
        "true", "1", "yes", "on", "enabled"
    }


def validate_bible(bible: dict, scenes: list[dict]) -> None:
    if bible.get("status") not in {"completed", "disabled"}:
        raise RuntimeError("Character Bible has an invalid status.")

    characters = bible.get("characters")
    scene_map = bible.get("scene_character_map")
    scene_roles = bible.get("scene_roles")

    if not isinstance(characters, list):
        raise RuntimeError("characters must be a list.")
    if not isinstance(scene_map, list):
        raise RuntimeError("scene_character_map must be a list.")
    if not isinstance(scene_roles, list):
        raise RuntimeError("scene_roles must be a list.")

    if bible["status"] == "disabled":
        if characters or scene_map or scene_roles:
            raise RuntimeError(
                "Disabled Character Bible must not contain character mappings."
            )
        return

    character_ids = set()
    character_names = set()
    source_ids = set()

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
            raise RuntimeError(f"Duplicate character ID: {character_id}")
        if name.casefold() in character_names:
            raise RuntimeError(f"Duplicate character name: {name}")
        if item.get("character_id") != character_id:
            raise RuntimeError(f"character_id mismatch for {name}.")

        character_ids.add(character_id)
        character_names.add(name.casefold())

        if not clean(item.get("visual_identity")):
            raise RuntimeError(f"Missing visual_identity for {name}.")

        rules = item.get("consistency_rules")
        constraints = item.get("physical_constraints")
        evidence = item.get("gender_evidence")
        scene_details = item.get("scene_details")

        if not isinstance(rules, list) or not rules:
            raise RuntimeError(f"Missing consistency rules for {name}.")
        if not isinstance(constraints, list) or not constraints:
            raise RuntimeError(f"Missing physical constraints for {name}.")
        if not isinstance(evidence, list):
            raise RuntimeError(f"Invalid gender evidence for {name}.")
        if not isinstance(scene_details, list):
            raise RuntimeError(f"Invalid scene details for {name}.")

        if item.get("gender") not in {
            "male", "female", "non_binary", "unspecified"
        }:
            raise RuntimeError(f"Invalid gender value for {name}.")

        if not isinstance(item.get("scene_ids"), list):
            raise RuntimeError(f"Invalid scene_ids for {name}.")
        if not isinstance(item.get("scene_keys"), list):
            raise RuntimeError(f"Invalid scene_keys for {name}.")

        for source_id in item.get("source_character_ids", []):
            key = str(source_id).casefold()
            if key in source_ids:
                raise RuntimeError(
                    f"Duplicate source character ID: {source_id}"
                )
            source_ids.add(key)

        for detail in scene_details:
            if not isinstance(detail, dict):
                raise RuntimeError(
                    f"Invalid scene-specific detail for {name}."
                )
            if not clean(detail.get("scene_id")):
                raise RuntimeError(
                    f"Scene-specific detail for {name} has no scene_id."
                )
            if not isinstance(detail.get("details"), dict):
                raise RuntimeError(
                    f"Scene-specific details for {name} are invalid."
                )

        for evidence_row in evidence:
            if not isinstance(evidence_row, dict):
                raise RuntimeError(
                    f"Invalid gender evidence record for {name}."
                )
            if evidence_row.get("value") not in {
                "male", "female", "non_binary"
            }:
                raise RuntimeError(
                    f"Invalid gender evidence value for {name}."
                )
            if not clean(evidence_row.get("source")):
                raise RuntimeError(
                    f"Gender evidence source missing for {name}."
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

    def validate_mapping_rows(rows: list, label: str) -> None:
        actual = set()

        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError(f"Invalid {label} row.")

            key = (
                row.get("part"),
                row.get("scene"),
                row.get("scene_id"),
                row.get("role"),
            )

            if key in actual:
                raise RuntimeError(
                    f"Duplicate {label} mapping: {key}"
                )
            actual.add(key)

            ids = row.get("character_ids", [])
            names = row.get("character_names", [])

            if not isinstance(ids, list) or not isinstance(names, list):
                raise RuntimeError(
                    f"Invalid character mapping arrays for {key}."
                )

            if len(ids) != len(set(ids)):
                raise RuntimeError(
                    f"Duplicate character IDs in mapping {key}."
                )

            unknown = [
                value for value in ids
                if value not in character_ids
            ]
            if unknown:
                raise RuntimeError(
                    f"Unknown character IDs in {key}: {unknown}"
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
                    f"Character names/IDs mismatch in {label} row {key}."
                )

            if row.get("characters") != expected_names:
                raise RuntimeError(
                    f"Character alias list mismatch in {label} row {key}."
                )

        if actual != expected:
            missing = expected - actual
            extra = actual - expected
            raise RuntimeError(
                f"{label} coverage mismatch: "
                f"missing={len(missing)}, extra={len(extra)}."
            )

    validate_mapping_rows(scene_map, "scene_character_map")
    validate_mapping_rows(scene_roles, "scene_roles")

    if len(scene_map) != len(scene_roles):
        raise RuntimeError("scene_roles and scene_character_map count mismatch.")

    if bible.get("character_count") != len(characters):
        raise RuntimeError("Character count metadata mismatch.")
    if bible.get("scene_count") != len(scenes):
        raise RuntimeError("Scene count metadata mismatch.")

    main_count = sum(row["role"] == "main" for row in scenes)
    extra_count = len(scenes) - main_count

    if bible.get("main_scene_count") != main_count:
        raise RuntimeError("Main scene count metadata mismatch.")
    if bible.get("extra_scene_count") != extra_count:
        raise RuntimeError("Extra scene count metadata mismatch.")

    if not isinstance(bible.get("world"), dict):
        raise RuntimeError("World continuity data is missing.")

    world_map = bible["world"].get("scene_world_map")
    if not isinstance(world_map, list) or len(world_map) != len(scenes):
        raise RuntimeError("World scene mapping is incomplete.")

    world_ids = [clean(row.get("scene_id")) for row in world_map]
    expected_ids = [row["scene_id"] for row in scenes]
    if world_ids != expected_ids:
        raise RuntimeError("World scene mapping does not match scene order.")


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
        "topic": clean(get_topic(config) or story.get("topic") or story.get("title")),
        "title": clean(story.get("title")),
        "format": normalize_format(config),
        "character_count": 0,
        "scene_count": len(scenes),
        "main_scene_count": sum(row["role"] == "main" for row in scenes),
        "extra_scene_count": sum(row["role"] != "main" for row in scenes),
        "characters": [],
        "scene_character_map": [],
        "scene_roles": [],
        "world": {},
        "warnings": ["CHARACTER_BIBLE is disabled in Input/topic.txt."],
    }


def build_bible(
    config: dict,
    story: dict,
    source_path: Path,
    scenes: list[dict],
    supplemental_story: dict | None = None,
) -> dict:
    topic = clean(
        get_topic(config) or story.get("topic") or story.get("title")
    )

    found = aggregate_characters(
        scenes, story, supplemental_story
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

    scene_map = build_scene_character_map(scenes, characters)
    scene_roles = build_scene_roles(scene_map)
    world = build_world(scenes)

    fingerprint_data = {
        "version": VERSION,
        "story": story,
        "supplemental_story": supplemental_story or {},
        "format": normalize_format(config),
        "character_consistency": config.get("CHARACTER_CONSISTENCY", True),
        "world_consistency": config.get("WORLD_CONSISTENCY", True),
        "scenes": [
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
            "No reliable character names or explicit cast records were found."
        )

    if any(item["gender"] == "unspecified" for item in characters):
        warnings.append(
            "Some character genders remain unspecified because evidence "
            "was missing or conflicting."
        )

    if any(not row["characters"] for row in scene_map):
        warnings.append(
            "Some scenes have no reliably identifiable character; "
            "no character was assigned by guesswork."
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
        "main_scene_count": sum(row["role"] == "main" for row in scenes),
        "extra_scene_count": sum(row["role"] != "main" for row in scenes),
        "characters": characters,
        "scene_character_map": scene_map,
        "scene_roles": scene_roles,
        "world": world,
        "warnings": warnings,
        "limitations": [
            "No external AI calls are made in this stage.",
            "Names are taken from explicit scene fields, clear naming phrases and declared cast records.",
            "Main, Hook, Suspense and Resolution scenes are all examined.",
            "Visual prompts are not treated as proof of character identity.",
            "Gender uses explicit fields first, then nearby Hindi/English grammatical evidence.",
            "Conflicting or insufficient gender evidence remains unspecified.",
            "Unspecified physical traits are not fabricated.",
            "Scene-specific clothing and physical changes remain attached to their source scene.",
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
        raise RuntimeError(f"Input file is missing: {input_path}")

    config = load_input_config(input_path)
    normalize_format(config)

    story, source_path = get_story()
    scenes = collect_scenes(story)
    supplemental_story = collect_supplemental_source()

    enabled = parse_enabled(config.get("CHARACTER_BIBLE"), default=True)

    if not enabled:
        bible = build_disabled_bible(
            config, story, source_path, scenes
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
            log("Existing Character Bible is invalid; rebuilding:", exc)

    write_json(OUTPUT_FILE, bible)

    # Verify the serialized output rather than trusting in-memory data.
    written = read_json(OUTPUT_FILE, required=True)
    if written.get("source_fingerprint") != bible["source_fingerprint"]:
        raise RuntimeError("Character Bible fingerprint read-back mismatch.")

    validate_bible(written, scenes)

    log("Character Bible created.")
    log("Version:", VERSION)
    log("Source:", source_path.relative_to(ROOT))
    log("Scenes examined:", len(scenes))
    log("Main scenes:", written["main_scene_count"])
    log("Extra scenes:", written["extra_scene_count"])
    log("Characters detected:", written["character_count"])
    log("Scene mappings:", len(written["scene_character_map"]))
    log("API calls:", written["api_calls"])
    log("Output:", OUTPUT_FILE.relative_to(ROOT))

    for warning in written["warnings"]:
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