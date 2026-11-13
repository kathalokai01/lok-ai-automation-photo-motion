#!/usr/bin/env python3
"""Build a source-grounded Character Bible and scene continuity maps."""

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

VERSION = "character-bible-v10-repeated-proper-name-extraction"

ROLE_FIELDS = (
    ("hook_scene", "hook"),
    ("suspense_scene", "suspense"),
    ("resolution_scene", "resolution"),
)

DETAIL_FIELDS = (
    "age", "gender", "appearance", "clothing", "hair", "eyes",
    "face_features", "skin_tone", "body_features",
    "distinctive_features", "personality", "role",
    "voice_description", "character_description", "height",
    "build", "accessories",
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
    "character_bible",
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
    "father": "male", "son": "male",
    "female": "female", "woman": "female", "girl": "female",
    "महिला": "female", "औरत": "female", "लड़की": "female",
    "लड़की": "female", "पुत्री": "female", "बेटी": "female",
    "mother": "female", "daughter": "female",
    "non-binary": "non_binary", "nonbinary": "non_binary",
    "अन्य": "non_binary",
}

HINDI_MALE_MARKERS = re.compile(
    r"(?:गया|आया|बैठा|उठा|बोला|कहा|करता|रहा|था|हुआ|चुका|सकता)"
)

HINDI_FEMALE_MARKERS = re.compile(
    r"(?:गई|आई|बैठी|उठी|बोली|कही|करती|रही|थी|हुई|चुकी|सकती)"
)

ENGLISH_MALE_MARKERS = re.compile(
    r"\b(?:he|him|his|himself)\b", re.IGNORECASE
)

ENGLISH_FEMALE_MARKERS = re.compile(
    r"\b(?:she|her|hers|herself)\b", re.IGNORECASE
)

PHYSICAL_HINTS = re.compile(
    r"(?:उम्र|वर्ष|साल|कद|लंबाई|लम्बाई|कद-काठी|"
    r"चेहरा|आँख|आंख|बाल|दाढ़ी|मूँछ|मूंछ|रंग|त्वचा|"
    r"शरीर|कद-काठी|कपड़े|पोशाक|जैकेट|साड़ी|साडी|"
    r"age|years old|height|tall|short|face|eyes|hair|"
    r"beard|moustache|skin|complexion|body|build|clothing|"
    r"wearing|dress|jacket|shirt|uniform|scar|mark)",
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
    return not any(token in STOP_NAMES for token in name.split())


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

        key = name.casefold()
        if valid_name(name) and key not in seen:
            seen.add(key)
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
        raw.get("characters") or raw.get("character_names") or raw.get("cast")
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
        "story_anchor": clean(raw.get("story_anchor")),
        "beat": clean(raw.get("beat")),
        "purpose": clean(raw.get("purpose")),
        "visual_prompt": visual_prompt,
        "location": clean(
            raw.get("location") or raw.get("setting")
            or raw.get("world_context")
        ),
        "time_period": clean(raw.get("time_period")),
        "characters": names,
        "character_ids": ids,
        "raw": raw,
        "raw_records": [raw],
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
                raise RuntimeError(f"Part {part_number} cannot contain {role}.")
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
                row["scene_id"]: row for row in result
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

                # Extra records enrich the canonical scene; they do not
                # create duplicate scene IDs or duplicate scene mappings.
                canonical = allowed[extra_id]
                canonical["raw_records"].append(extra)

                for field in (
                    "characters", "character_names", "cast",
                    "character_ids", "story_anchor", "beat", "purpose",
                    "narration", "dialogue", "location", "setting",
                    "time_period", *DETAIL_FIELDS,
                ):
                    if extra.get(field) not in (None, "", [], {}):
                        if field in {"characters", "character_names", "cast"}:
                            canonical["characters"] = list(dict.fromkeys(
                                canonical["characters"]
                                + normalize_names(extra.get(field))
                            ))
                        elif field == "character_ids":
                            values = extra[field]
                            if isinstance(values, (str, int)):
                                values = [values]
                            if isinstance(values, list):
                                canonical["character_ids"] = list(dict.fromkeys(
                                    canonical["character_ids"]
                                    + [clean(value) for value in values if clean(value)]
                                ))
                        elif field in {
                            "story_anchor", "beat", "purpose", "narration",
                            "dialogue", "location", "setting", "time_period",
                        }:
                            if not canonical.get(field):
                                canonical[field] = clean(extra[field])

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
    values = [
        clean(row.get("title")),
        clean(row.get("narration")),
        clean(row.get("dialogue")),
        clean(row.get("story_anchor")),
        clean(row.get("beat")),
        clean(row.get("purpose")),
    ]

    for raw in row.get("raw_records", [row.get("raw", {})]):
        if not isinstance(raw, dict):
            continue
        values.extend(
            clean(raw.get(field))
            for field in (
                "title", "narration", "dialogue", "story_anchor",
                "beat", "purpose", "description", "story_text",
            )
        )

    return " ".join(value for value in values if value)


def extract_explicit_names(row: dict) -> list[str]:
    text = source_text(row)
    candidates = set()

    for pattern in HINDI_NAME_PATTERNS + ENGLISH_NAME_PATTERNS:
        for match in pattern.findall(text):
            name = normalize_name(match)
            if valid_name(name):
                candidates.add(name)

    for raw in row.get("raw_records", [row.get("raw", {})]):
        if not isinstance(raw, dict):
            continue
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
            records.extend(item for item in value if isinstance(item, dict))
    return records


def collect_story_text(story: dict | None) -> str:
    if not isinstance(story, dict):
        return ""

    fields = (
        "corrected_complete_story", "story_text", "complete_story",
        "full_story", "story", "content", "narration",
    )
    values = [clean(story.get(field)) for field in fields]
    return "\n".join(value for value in values if value)


def extract_story_names(text: str) -> list[str]:
    candidates = set()
    source = clean(text)
    for pattern in HINDI_NAME_PATTERNS + ENGLISH_NAME_PATTERNS:
        for match in pattern.findall(source):
            name = normalize_name(match)
            if valid_name(name):
                candidates.add(name)

    # Stories frequently introduce English character names directly
    # ("Ayaan entered..." / "Vikrant Mehra called...") without "named".
    # Recover repeated proper-name forms while excluding common sentence
    # openers and generic words. Requiring repetition reduces false cast IDs.
    proper_pattern = re.compile(
        r"\\b[A-Z][a-z]{2,}(?:[-'][A-Za-z0-9]+)?"
        r"(?:\\s+[A-Z][a-z]{2,}){0,2}\\b"
    )
    for match in proper_pattern.finditer(source):
        name = normalize_name(match.group(0))
        if not valid_name(name):
            continue
        first = name.split()[0].casefold()
        if first in GENERIC_ENGLISH:
            continue
        occurrences = len(re.findall(
            r"(?<![\\w])" + re.escape(name) + r"(?![\\w])",
            source,
            flags=re.IGNORECASE,
        ))
        if occurrences >= 2:
            candidates.add(name)

    return sorted(candidates, key=str.casefold)


def new_character(name: str) -> dict:
    return {
        "name": name,
        "mention_count": 0,
        "scene_ids": [],
        "scene_keys": [],
        "aliases": [],
        "detail_values": {field: [] for field in DETAIL_FIELDS},
        "scene_details": [],
        "source_descriptions": [],
        "explicit_gender_values": [],
        "gender_evidence": [],
        "source_character_ids": [],
    }


def add_detail(target: dict, field: str, value: Any) -> None:
    if field not in DETAIL_FIELDS:
        return
    value = clean(value)
    if not value:
        return
    values = target["detail_values"].setdefault(field, [])
    if value not in values:
        values.append(value)


def sentence_rows(text: str) -> list[str]:
    return [
        clean(item)
        for item in re.split(r"(?<=[।.!?])\s+|[\n]+", text)
        if clean(item)
    ]


def name_pattern(name: str) -> re.Pattern:
    return re.compile(
        r"(?<![\w])" + re.escape(name) + r"(?![\w])",
        re.IGNORECASE,
    )


def nearby_gender_evidence(
    name: str,
    text: str,
    scene_id: str,
    known_names: list[str],
) -> list[dict]:
    """Attach gender evidence only when grammatical markers are local to one name."""
    evidence = []
    target_pattern = name_pattern(name)

    other_patterns = [
        name_pattern(other)
        for other in known_names
        if other.casefold() != name.casefold()
    ]

    for sentence in sentence_rows(text):
        matches = list(target_pattern.finditer(sentence))
        if not matches:
            continue

        for match in matches:
            start = max(0, match.start() - 45)
            end = min(len(sentence), match.end() + 45)
            window = sentence[start:end]

            # If another known character is also in this local window,
            # the marker is ambiguous and is not attributed to either.
            if any(pattern.search(window) for pattern in other_patterns):
                continue

            male = bool(
                HINDI_MALE_MARKERS.search(window)
                or ENGLISH_MALE_MARKERS.search(window)
            )
            female = bool(
                HINDI_FEMALE_MARKERS.search(window)
                or ENGLISH_FEMALE_MARKERS.search(window)
            )

            if male == female:
                continue

            value = "male" if male else "female"
            evidence.append({
                "value": value,
                "source": "local grammatical evidence near one named character",
                "confidence": "contextual",
                "scene_id": scene_id,
                "evidence_text": sentence[:240],
            })

    return evidence


def extract_physical_details(
    name: str,
    text: str,
    target: dict,
    scene_id: str = "",
) -> dict:
    """Keep source wording; never infer an unstated physical attribute."""
    captured = {}

    for sentence in sentence_rows(text):
        if not name_pattern(name).search(sentence):
            continue
        if not PHYSICAL_HINTS.search(sentence):
            continue

        if sentence not in target["source_descriptions"]:
            target["source_descriptions"].append(sentence)

        add_detail(target, "character_description", sentence)
        add_detail(target, "appearance", sentence)

        # Explicit age patterns only; no age is calculated or guessed.
        age_patterns = (
            r"(?:उम्र|आयु)\s*(?:करीब|लगभग)?\s*(\d{1,3})\s*(?:वर्ष|साल)",
            r"\b(\d{1,3})[- ]year[- ]old\b",
            r"\baged\s+(\d{1,3})\b",
        )
        for pattern in age_patterns:
            match = re.search(pattern, sentence, re.IGNORECASE)
            if match:
                value = match.group(0)
                add_detail(target, "age", value)
                captured["age"] = value
                break

        # Preserve exact source sentences under relevant physical fields.
        field_terms = {
            "hair": r"(?:बाल|दाढ़ी|मूँछ|मूंछ|hair|beard|moustache)",
            "eyes": r"(?:आँख|आंख|eyes?)",
            "face_features": r"(?:चेहरा|नाक|माथा|face|nose|forehead)",
            "skin_tone": r"(?:त्वचा|रंग|skin|complexion)",
            "body_features": r"(?:शरीर|कद-काठी|body|build|physique)",
            "height": r"(?:कद|लंबाई|लम्बाई|height|tall|short)",
            "clothing": r"(?:कपड़े|पोशाक|साड़ी|साडी|जैकेट|"
                         r"clothing|wearing|dress|jacket|shirt|uniform)",
            "distinctive_features": r"(?:निशान|दाग|scar|mark|distinctive)",
        }

        for field in PHYSICAL_FIELDS:
            if field not in field_terms:
                continue
            if re.search(field_terms[field], sentence, re.IGNORECASE):
                add_detail(target, field, sentence)
                captured[field] = sentence

        if scene_id:
            captured["scene_id"] = scene_id

    return captured


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


def add_occurrence(
    target: dict,
    row: dict,
    name: str,
    known_names: list[str],
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

    scene_detail = {
        "scene_id": row["scene_id"],
        "part": row["part"],
        "scene": row["scene"],
        "role": row["role"],
        "details": {},
    }

    for raw in row.get("raw_records", [row.get("raw", {})]):
        if not isinstance(raw, dict):
            continue

        for field in DETAIL_FIELDS:
            value = clean(raw.get(field))
            if value:
                add_detail(target, field, value)
                scene_detail["details"][field] = value

        declared = explicit_gender(raw.get("gender"))
        if declared:
            target["explicit_gender_values"].append(declared)
            target["gender_evidence"].append({
                "value": declared,
                "source": "explicit scene field",
                "confidence": "explicit",
                "scene_id": row["scene_id"],
            })

    text = source_text(row)
    target["gender_evidence"].extend(
        nearby_gender_evidence(
            name, text, row["scene_id"], known_names
        )
    )

    physical = extract_physical_details(
        name, text, target, row["scene_id"]
    )
    for field, value in physical.items():
        if field in DETAIL_FIELDS and value:
            scene_detail["details"][field] = value

    if scene_detail["details"]:
        existing = next(
            (
                item for item in target["scene_details"]
                if item.get("scene_id") == row["scene_id"]
            ),
            None,
        )
        if existing:
            existing["details"].update(scene_detail["details"])
        else:
            target["scene_details"].append(scene_detail)

    for source_id in row.get("character_ids", []):
        if source_id not in target["source_character_ids"]:
            target["source_character_ids"].append(source_id)

def aggregate_characters(
    scenes: list[dict],
    story: dict,
    supplemental_story: dict | None,
) -> dict[str, dict]:
    found: dict[str, dict] = {}

    # Explicit cast records from AI story and original story.
    for raw in collect_global_records(story):
        add_global_character_record(
            found, raw, "explicit AI story cast record"
        )

    if supplemental_story:
        for raw in collect_global_records(supplemental_story):
            add_global_character_record(
                found, raw, "explicit source story cast record"
            )

    # Discover names from the complete corrected source story.
    full_story_text = collect_story_text(supplemental_story)
    story_names = extract_story_names(full_story_text)

    for name in story_names:
        key = name.casefold()
        if key not in found:
            found[key] = new_character(name)

    # Discover names from every Main, Hook, Suspense and Resolution scene.
    for row in scenes:
        for name in extract_explicit_names(row):
            key = name.casefold()
            if key not in found:
                found[key] = new_character(name)

    known_names = []
    for item in found.values():
        for name in [item["name"], *item["aliases"]]:
            if name and name not in known_names:
                known_names.append(name)

    # Connect complete-story physical descriptions to the exact named person.
    for item in found.values():
        names = [item["name"], *item["aliases"]]
        for name in names:
            extract_physical_details(
                name, full_story_text, item
            )

    # Count only characters explicitly referenced by a scene's source text,
    # character list, or known source character ID.
    for row in scenes:
        scene_text = source_text(row)
        scene_names = set(
            name.casefold() for name in row.get("characters", [])
        )

        for item in found.values():
            candidates = [item["name"], *item["aliases"]]
            matched = False

            for candidate in candidates:
                if name_pattern(candidate).search(scene_text):
                    matched = True
                    break
                if candidate.casefold() in scene_names:
                    matched = True
                    break

            if not matched:
                source_ids = {
                    str(value).casefold()
                    for value in item["source_character_ids"]
                }
                if any(
                    str(value).casefold() in source_ids
                    for value in row.get("character_ids", [])
                ):
                    matched = True

            if matched:
                matched_name = next(
                    (
                        candidate for candidate in candidates
                        if name_pattern(candidate).search(scene_text)
                        or candidate.casefold() in scene_names
                    ),
                    item["name"],
                )
                add_occurrence(
                    item, row, matched_name, known_names
                )

    return found


def choose_gender(found: dict) -> tuple[str, str]:
    explicit = {
        value for value in found["explicit_gender_values"]
        if value in {"male", "female", "non_binary"}
    }
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
            "consistent local Hindi/English grammatical evidence",
        )
    if len(contextual) > 1:
        return "unspecified", "conflicting local gender evidence"

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
        "Source-supported variants; apply only where the matching "
        "story scene supports them: " + " | ".join(unique[:30])
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
        "character_description": supported_detail(
            values["character_description"],
            "No explicit physical description found in source.",
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
        "source_descriptions": found["source_descriptions"][:100],
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
            "Use the complete-story description as identity evidence, not as permission to invent traits.",
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
    character_by_id = {item["id"]: item for item in characters}

    for character in characters:
        character_id = character["id"]

        for name in [
            character.get("name", ""),
            *character.get("aliases", []),
        ]:
            normalized = normalize_name(name)
            if not normalized:
                continue
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

        # Explicit scene character names and aliases.
        for name in references:
            character_id = name_lookup.get(
                normalize_name(name).casefold()
            )
            if character_id and character_id not in resolved_ids:
                resolved_ids.append(character_id)

        # Complete-name matching against narration, dialogue, story anchor,
        # beat and purpose. Visual prompt is intentionally not identity proof.
        for character in characters:
            candidates = [
                character.get("name", ""),
                *character.get("aliases", []),
            ]
            for candidate in candidates:
                candidate = normalize_name(candidate)
                if not candidate:
                    continue
                if name_pattern(candidate).search(text):
                    if character["id"] not in resolved_ids:
                        resolved_ids.append(character["id"])
                    break

        # Source IDs resolve only to an explicitly declared cast record.
        for source_id in row.get("character_ids", []):
            character_id = source_id_lookup.get(
                str(source_id).casefold()
            )
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
    # Compatibility view generated from the one canonical scene map.
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
            "story_anchor": row["story_anchor"],
            "beat": row["beat"],
            "purpose": row["purpose"],
            "continuity_rule": (
                "Use only the source location, time period and scene context. "
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

        for field in (
            "consistency_rules", "physical_constraints",
            "gender_evidence", "scene_details", "source_descriptions",
            "scene_ids", "scene_keys", "aliases", "source_character_ids",
        ):
            if not isinstance(item.get(field), list):
                raise RuntimeError(f"Invalid {field} for {name}.")

        if item.get("gender") not in {
            "male", "female", "non_binary", "unspecified"
        }:
            raise RuntimeError(f"Invalid gender value for {name}.")

        for source_id in item.get("source_character_ids", []):
            key = str(source_id).casefold()
            if key in source_ids:
                raise RuntimeError(
                    f"Duplicate source character ID: {source_id}"
                )
            source_ids.add(key)

        for detail in item.get("scene_details", []):
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

        for evidence_row in item.get("gender_evidence", []):
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
                raise RuntimeError(f"Duplicate {label} mapping: {key}")
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

    if scene_roles != build_scene_roles(scene_map):
        raise RuntimeError(
            "scene_roles must be the compatibility view of scene_character_map."
        )

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
        "topic": clean(
            get_topic(config) or story.get("topic") or story.get("title")
        ),
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
    # Stable voice identity metadata; actual provider voice IDs are resolved later.
    for item in characters:
        gender = clean(item.get("gender", "unspecified")).lower()
        item["voice_identity"] = {
            "speaker_id": clean(item.get("id")),
            "speaker_name": clean(item.get("name")),
            "voice_style": clean(item.get("voice_description")) or "distinct, intelligible character performance",
            "gender_presentation": gender if gender in {"male", "female", "non_binary"} else "unspecified",
            "provider_voice_id": None,
            "identity_locked": True,
        }

    scene_map = build_scene_character_map(scenes, characters)
    scene_roles = build_scene_roles(scene_map)
    world = build_world(scenes)

    fingerprint_data = {
        "version": VERSION,
        "story": story,
        "supplemental_story": supplemental_story or {},
        "format": normalize_format(config),
        "character_consistency": config.get(
            "CHARACTER_CONSISTENCY", True
        ),
        "world_consistency": config.get("WORLD_CONSISTENCY", True),
        "scenes": [
            {
                "scene_id": row["scene_id"],
                "part": row["part"],
                "scene": row["scene"],
                "role": row["role"],
                "narration": row["narration"],
                "dialogue": row["dialogue"],
                "story_anchor": row["story_anchor"],
                "beat": row["beat"],
                "purpose": row["purpose"],
                "visual_prompt": row["visual_prompt"],
                "characters": row["characters"],
                "character_ids": row["character_ids"],
                "location": row["location"],
                "time_period": row["time_period"],
                "raw_records": row["raw_records"],
            }
            for row in scenes
        ],
    }

    warnings = []

    if not characters:
        warnings.append(
            "No reliable character names or explicit cast records were found."
        )
        if any(
            extract_story_names(collect_story_text(candidate))
            for candidate in (story, supplemental_story)
            if isinstance(candidate, dict)
        ) or any(extract_explicit_names(row) for row in scenes):
            raise RuntimeError(
                "Character Bible is empty although named characters are present "
                "in the complete story or scene narration."
            )

    if any(item["gender"] == "unspecified" for item in characters):
        warnings.append(
            "Some character genders remain unspecified because evidence "
            "was missing, ambiguous or conflicting."
        )

    if any(not row["characters"] for row in scene_map):
        warnings.append(
            "Some scenes have no reliably identifiable character; "
            "no character was assigned by guesswork."
        )

    bible = {
        "status": "completed",
        "version": VERSION,
        "generation_mode": "offline_full_story_source_grounded",
        "api_calls": 0,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "source_story_file": (
            SOURCE_STORY_FILE.relative_to(ROOT).as_posix()
            if supplemental_story else None
        ),
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
            "Names are extracted from explicit scene fields, clear naming phrases and declared cast records.",
            "The corrected complete source story is also examined for named characters and physical descriptions.",
            "Scene anchors, beats and purposes are used as source context for character matching.",
            "Visual prompts are not treated as proof of character identity.",
            "Gender uses explicit fields first, then local grammatical evidence tied to one named character.",
            "Ambiguous or conflicting gender evidence remains unspecified.",
            "Physical details are preserved only when supported by source text or explicit fields.",
            "Scene-specific clothing and physical changes remain attached to their source scene.",
            "Extra scene records enrich their matching canonical scene and do not create duplicate scene IDs.",
            "scene_roles is a compatibility view derived from scene_character_map.",
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

    enabled = parse_enabled(
        config.get("CHARACTER_BIBLE"), default=True
    )

    if not enabled:
        bible = build_disabled_bible(
            config, story, source_path, scenes
        )
        write_json(OUTPUT_FILE, bible)
        log("Character Bible disabled by Input/topic.txt.")
        log("Output:", OUTPUT_FILE)
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

    # Verify serialized output rather than trusting in-memory data.
    written = read_json(OUTPUT_FILE, required=True)
    if written.get("source_fingerprint") != bible["source_fingerprint"]:
        raise RuntimeError(
            "Character Bible fingerprint read-back mismatch."
        )

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