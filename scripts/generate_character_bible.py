#!/usr/bin/env python3
"""Build a source-grounded character bible for the Photo Motion pipeline.

No AI/API calls are made. Character identity and appearance are taken only
from story narration, dialogue and explicit character records.
"""

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

VERSION = "character-bible-v5-source-grounded"
NAME_FIELDS = (
    "character_name", "person_name", "protagonist",
    "character", "name",
)
DETAIL_FIELDS = (
    "age", "appearance", "clothing", "hair", "eyes",
    "face_features", "skin_tone", "body_features",
    "distinctive_features", "personality", "role",
    "gender", "voice_description", "character_description",
)
EXPLICIT_CHARACTER_KEYS = (
    "characters", "character_list", "cast",
    "main_characters", "character_bible",
)

DEVANAGARI = r"[\u0900-\u097F]+"
HINDI_NAME_PATTERNS = (
    re.compile(
        rf"(?:नाम\s*(?:था|थी|है)|जिसका नाम|जिसका नाम था|"
        rf"जिसका नाम था कि)\s*"
        rf"({DEVANAGARI}(?:\s+{DEVANAGARI}){{0,2}})"
    ),
    re.compile(
        rf"(?:श्री|श्रीमती|सुश्री|डॉ\.?|डॉक्टर|मिस्टर|मिस|"
        rf"कप्तान|इंस्पेक्टर|प्रोफेसर)\s+"
        rf"({DEVANAGARI}(?:\s+{DEVANAGARI}){{0,2}})"
    ),
)

ENGLISH_EXPLICIT_PATTERNS = (
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

HINDI_STOP_NAMES = {
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

MALE_CUES = (
    "लड़का", "लड़के", "लड़का", "लड़के", "पुरुष",
    "आदमी", "बेटा", "पुत्र", "पिता", "बाप",
    "भाई", "पति", "दूल्हा", "राजा", "महाराजा",
    "सम्राट", "नायक", "युवक", "बोला", "कहता है",
    "male", "man", "boy", "son", "father",
    "brother", "husband", "king", "prince",
)

FEMALE_CUES = (
    "लड़की", "लड़कियां", "लड़कियाँ", "लड़की",
    "महिला", "औरत", "बेटी", "पुत्री", "माता",
    "माँ", "मां", "बहन", "पत्नी", "दुल्हन",
    "रानी", "महारानी", "युवती", "बोली",
    "कहती है", "female", "woman", "girl",
    "daughter", "mother", "sister", "wife",
    "queen", "princess",
)


def clean(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def log(*items: Any) -> None:
    print(*items, flush=True)


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


def file_fingerprint(data: Any) -> str:
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_story() -> tuple[dict, Path]:
    errors = []

    # Prefer the final generated scene story. Fall back to source story only
    # when AI story is unavailable or incomplete.
    for path in (AI_STORY_FILE, SOURCE_STORY_FILE):
        data = read_json(path)

        if not isinstance(data, dict):
            errors.append(f"{path.name} missing or invalid")
            continue

        status = clean(data.get("status")).lower()
        if status and status != "completed":
            errors.append(f"{path.name} status is {status}")
            continue

        parts = data.get("parts")
        if not isinstance(parts, list) or not parts:
            errors.append(f"{path.name} has no story parts")
            continue

        return data, path

    raise RuntimeError(
        "No usable completed story found: " + "; ".join(errors)
    )


def normalize_name(value: Any) -> str:
    name = clean(value).strip(
        ".,:;!?()[]{}'\"“”‘’।॥-–—"
    )
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

    if name in HINDI_STOP_NAMES:
        return False

    if not re.search(r"[A-Za-z\u0900-\u097F]", name):
        return False

    # Avoid accepting a sentence fragment as a character name.
    if any(token in HINDI_STOP_NAMES for token in name.split()):
        return False

    return True


def normalize_character_values(value: Any) -> list[str]:
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
            if not name:
                name = normalize_name(item.get("name"))
        else:
            name = normalize_name(item)

        if not valid_name(name):
            continue

        key = name.casefold()
        if key not in seen:
            seen.add(key)
            result.append(name)

    return result


def normalize_part_scene(part: dict, part_number: int) -> list[dict]:
    scenes = part.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError(
            f"Story part {part_number} has no valid scenes list."
        )

    result = []

    for scene_number, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise RuntimeError(
                f"Invalid scene at Part {part_number}, "
                f"Scene {scene_number}."
            )

        actual_number = scene.get("scene")
        if actual_number is not None and actual_number != scene_number:
            raise RuntimeError(
                f"Scene numbering mismatch in Part {part_number}: "
                f"expected {scene_number}, got {actual_number!r}."
            )

        scene_id = (
            clean(scene.get("scene_id"))
            or f"part_{part_number:02d}_scene_{scene_number:02d}"
        )

        result.append({
            "part": part_number,
            "scene": scene_number,
            "scene_id": scene_id,
            "title": clean(scene.get("title")),
            "narration": clean(
                scene.get("narration")
                or scene.get("text")
                or scene.get("story_text")
            ),
            "dialogue": clean(scene.get("dialogue")),
            "visual_prompt": clean(scene.get("visual_prompt")),
            "location": clean(
                scene.get("location")
                or scene.get("setting")
                or scene.get("world_context")
            ),
            "time_period": clean(scene.get("time_period")),
            "characters": normalize_character_values(
                scene.get("characters")
                or scene.get("character_names")
            ),
            "character_ids": normalize_character_values(
                scene.get("character_ids")
            ),
            "raw": scene,
        })

    return result


def collect_scenes(story: dict) -> list[dict]:
    parts = story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Story contains no parts.")

    rows = []
    seen_ids = set()

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part {part_number}.")

        declared_part = part.get("part")
        if declared_part is not None and declared_part != part_number:
            raise RuntimeError(
                f"Part numbering mismatch: expected {part_number}, "
                f"got {declared_part!r}."
            )

        for row in normalize_part_scene(part, part_number):
            if row["scene_id"] in seen_ids:
                raise RuntimeError(
                    f"Duplicate scene_id found: {row['scene_id']}"
                )
            seen_ids.add(row["scene_id"])

            if not row["narration"] and not row["dialogue"]:
                raise RuntimeError(
                    f"Part {part_number}, Scene {row['scene']} "
                    "has no narration or dialogue."
                )

            rows.append(row)

    if not rows:
        raise RuntimeError("Story contains no usable scenes.")

    return rows


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

    for pattern in ENGLISH_EXPLICIT_PATTERNS:
        for match in pattern.findall(text):
            name = normalize_name(match)
            if valid_name(name):
                candidates.add(name)

    # Explicit scene fields are stronger than inferred text candidates.
    for field in NAME_FIELDS:
        name = normalize_name(row["raw"].get(field))
        if valid_name(name):
            candidates.add(name)

    for name in row.get("characters", []):
        if valid_name(name):
            candidates.add(normalize_name(name))

    return sorted(candidates, key=str.casefold)


def collect_global_character_records(story: dict) -> list[dict]:
    records = []

    for key in EXPLICIT_CHARACTER_KEYS:
        value = story.get(key)

        if isinstance(value, dict):
            value = value.get("characters", [])

        if isinstance(value, list):
            records.extend(
                item for item in value
                if isinstance(item, dict)
            )

    return records


def explicit_gender(value: Any) -> str:
    return GENDER_ALIASES.get(clean(value).casefold(), "")


def nearby_gender_evidence(row: dict, name: str) -> list[dict]:
    """Use nearby grammar only as weak evidence; never guess from names."""
    text = source_text(row)
    if not text or not name:
        return []

    match = re.search(re.escape(name), text, flags=re.IGNORECASE)
    if not match:
        return []

    context = text[
        max(0, match.start() - 100):
        min(len(text), match.end() + 140)
    ].casefold()

    male_hits = [
        cue for cue in MALE_CUES
        if cue.casefold() in context
    ]
    female_hits = [
        cue for cue in FEMALE_CUES
        if cue.casefold() in context
    ]

    if male_hits and not female_hits:
        return [{
            "value": "male",
            "source": "nearby story wording",
            "confidence": "low",
            "cues": male_hits[:5],
        }]

    if female_hits and not male_hits:
        return [{
            "value": "female",
            "source": "nearby story wording",
            "confidence": "low",
            "cues": female_hits[:5],
        }]

    if male_hits and female_hits:
        return [{
            "value": "ambiguous",
            "source": "conflicting nearby story wording",
            "confidence": "ambiguous",
            "cues": male_hits[:3] + female_hits[:3],
        }]

    return []


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
        "gender_evidence": [],
        "explicit_gender_values": [],
    }


def add_detail(target: dict, field: str, value: Any) -> None:
    value = clean(value)
    if not value:
        return

    values = target["detail_values"].setdefault(field, [])
    if value not in values:
        values.append(value)


def add_character_occurrence(
    target: dict,
    row: dict,
    name: str,
) -> None:
    target["mention_count"] += 1

    if row["scene_id"] not in target["scene_ids"]:
        target["scene_ids"].append(row["scene_id"])

    key = {
        "part": row["part"],
        "scene": row["scene"],
        "scene_id": row["scene_id"],
    }
    if key not in target["scene_keys"]:
        target["scene_keys"].append(key)

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
            "source": "explicit scene gender field",
            "confidence": "explicit",
            "scene_id": row["scene_id"],
        })

    target["gender_evidence"].extend(
        nearby_gender_evidence(row, name)
    )


def aggregate_characters(
    scenes: list[dict],
    story: dict,
) -> dict[str, dict]:
    characters = {}

    # First pass: explicitly declared scene characters and names stated
    # through clear naming phrases in narration/dialogue.
    for row in scenes:
        for name in extract_explicit_names(row):
            key = name.casefold()
            item = characters.setdefault(key, new_character(name))
            add_character_occurrence(item, row, name)

    # Second pass: story-level cast records, preserving their exact details.
    for raw in collect_global_character_records(story):
        name = ""
        for field in NAME_FIELDS:
            name = normalize_name(raw.get(field))
            if name:
                break

        if not name:
            name = normalize_name(raw.get("name"))

        if not valid_name(name):
            continue

        key = name.casefold()
        item = characters.setdefault(key, new_character(name))

        for field in DETAIL_FIELDS:
            add_detail(item, field, raw.get(field))

        declared = explicit_gender(raw.get("gender"))
        if declared:
            item["explicit_gender_values"].append(declared)
            item["gender_evidence"].append({
                "value": declared,
                "source": "explicit story-level character record",
                "confidence": "explicit",
            })

        aliases = normalize_character_values(raw.get("aliases", []))
        for alias in aliases:
            if alias.casefold() != key and alias not in item["aliases"]:
                item["aliases"].append(alias)

    # Keep first-seen name casing and stable ordering.
    return dict(sorted(
        characters.items(),
        key=lambda pair: (
            -pair[1]["mention_count"],
            pair[1]["name"].casefold(),
        ),
    ))


def choose_gender(found: dict) -> tuple[str, str]:
    explicit = set(found["explicit_gender_values"])

    if len(explicit) == 1:
        return next(iter(explicit)), "explicit source field"

    if len(explicit) > 1:
        return "unspecified", "conflicting explicit source fields"

    weak = {
        item["value"]
        for item in found["gender_evidence"]
        if item.get("confidence") == "low"
        and item.get("value") in {"male", "female"}
    }

    ambiguous = any(
        item.get("value") == "ambiguous"
        for item in found["gender_evidence"]
    )

    # Weak grammatical cues are recorded but do not define identity.
    if ambiguous or len(weak) > 1:
        return "unspecified", "ambiguous nearby wording"

    return "unspecified", "no explicit gender field"


def supported_detail(values: list[str], fallback: str) -> str:
    unique = list(dict.fromkeys(
        clean(value) for value in values if clean(value)
    ))

    if not unique:
        return fallback

    if len(unique) == 1:
        return unique[0]

    return (
        "Scene-specific source variants; use the detail from the "
        "matching scene: " + " | ".join(unique[:12])
    )


def character_visual_identity(found: dict, gender: str) -> str:
    values = found["detail_values"]

    details = {
        "age": supported_detail(
            values["age"], "Not specified; do not invent."
        ),
        "gender": gender,
        "appearance": supported_detail(
            values["appearance"], "Not specified; follow scene source."
        ),
        "hair": supported_detail(
            values["hair"], "Not specified; do not invent."
        ),
        "eyes": supported_detail(
            values["eyes"], "Not specified; do not invent."
        ),
        "face": supported_detail(
            values["face_features"], "Not specified; do not invent."
        ),
        "skin_tone": supported_detail(
            values["skin_tone"], "Not specified; do not invent."
        ),
        "body": supported_detail(
            values["body_features"], "Not specified; do not invent."
        ),
        "clothing": supported_detail(
            values["clothing"], "Use the matching scene's clothing."
        ),
        "distinctive_features": supported_detail(
            values["distinctive_features"], "Not specified."
        ),
    }

    return (
        f"Character identity: {found['name']}. "
        + " ".join(
            f"{key.replace('_', ' ')}: {value}."
            for key, value in details.items()
        )
        + " Preserve the same recognizable identity across connected "
        "scenes. Use scene-specific source details when clothing or "
        "appearance changes are explicitly described. Do not invent "
        "missing physical traits."
    )


def build_character(found: dict, index: int) -> dict:
    gender, gender_reason = choose_gender(found)
    values = found["detail_values"]
    character_id = f"character_{index:03d}"

    role = supported_detail(
        values["role"], "Follow the character's actual story role."
    )
    appearance = supported_detail(
        values["appearance"], "Not specified in source."
    )
    clothing = supported_detail(
        values["clothing"], "Use explicit scene clothing only."
    )
    personality = supported_detail(
        values["personality"], "Not specified in source."
    )

    return {
        "id": character_id,
        "character_id": character_id,
        "name": found["name"],
        "aliases": found["aliases"],
        "role": role,
        "importance": (
            "recurring" if found["mention_count"] > 1 else "mentioned"
        ),
        "age": supported_detail(
            values["age"], "Not specified in source."
        ),
        "gender": gender,
        "gender_reason": gender_reason,
        "gender_evidence": found["gender_evidence"][:40],
        "personality": personality,
        "appearance": appearance,
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
            values["body_features"], "Not specified in source."
        ),
        "clothing": clothing,
        "distinctive_features": supported_detail(
            values["distinctive_features"], "Not specified in source."
        ),
        "visual_identity": character_visual_identity(found, gender),
        "continuity_notes": (
            "Preserve identity across scenes. Explicit scene-specific "
            "details take priority. Do not combine conflicting descriptions."
        ),
        "consistency_rules": [
            "Preserve the same recognizable face across connected scenes.",
            "Do not infer gender from a name.",
            "Do not invent age, skin tone, hair or clothing.",
            "Use explicit scene-specific appearance when available.",
            "Do not transfer one character's traits to another character.",
            "Keep realistic anatomy, natural skin texture and proportions.",
            "Keep clothing consistent unless the story describes a change.",
        ],
        "mention_count": found["mention_count"],
        "scene_ids": found["scene_ids"],
        "scene_keys": found["scene_keys"],
    }

def build_scene_character_map(
    scenes: list[dict],
    characters: list[dict],
) -> list[dict]:
    """Map stable character IDs to every scene where they are supported."""

    lookup = {}

    for character in characters:
        character_id = character["id"]

        names = [
            character.get("name", ""),
            *character.get("aliases", []),
        ]

        for name in names:
            normalized = normalize_name(name)
            if normalized:
                lookup[normalized.casefold()] = character_id

    result = []

    for row in scenes:
        text = source_text(row)
        explicit_names = list(row.get("characters", []))

        # Include names explicitly mentioned in narration/dialogue.
        # Word boundaries are used for Latin names to avoid partial matches.
        for character in characters:
            names = [
                character.get("name", ""),
                *character.get("aliases", []),
            ]

            matched = False

            for name in names:
                name = normalize_name(name)
                if not name:
                    continue

                if re.search(
                    re.escape(name),
                    text,
                    flags=re.IGNORECASE,
                ):
                    matched = True
                    break

            if matched:
                explicit_names.append(character["name"])

        # Preserve explicit scene character references even if the name
        # does not appear in the narration itself.
        ids = []
        names = []

        for name in explicit_names:
            normalized = normalize_name(name)
            if not normalized:
                continue

            character_id = lookup.get(normalized.casefold())
            if character_id and character_id not in ids:
                ids.append(character_id)

            canonical = next(
                (
                    item["name"]
                    for item in characters
                    if item["id"] == character_id
                ),
                normalized,
            )

            if canonical not in names:
                names.append(canonical)

        # If the scene carries character IDs from an earlier pipeline stage,
        # preserve them only when they match this bible's stable IDs.
        for old_id in row.get("character_ids", []):
            if old_id in {
                item["id"] for item in characters
            } and old_id not in ids:
                ids.append(old_id)

        result.append({
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "character_ids": ids,
            "character_names": names,
            "characters": names,
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
        "setting": "; ".join(locations[:40]) or "Not specified",
        "time_period": "; ".join(periods[:20]) or "Not specified",
        "geography": "Use only locations supported by the story.",
        "architecture": "Match the source scene and time period.",
        "environment": "Follow scene descriptions and continuity.",
        "weather_style": "Use explicit source details only.",
        "color_and_lighting": (
            "Natural cinematic lighting, realistic skin tones, "
            "moderate contrast and visible shadow detail."
        ),
        "continuity_rules": [
            "Keep connected locations and time periods consistent.",
            "Avoid anachronistic clothing, technology and architecture.",
            "Do not add events that contradict the story.",
            "Prefer explicit scene descriptions over assumptions.",
            "Keep recurring locations visually recognizable.",
            "Do not force one weather condition across unrelated scenes.",
        ],
    }


def collect_supplemental_source():
    """Load source story separately to retain original cast declarations."""
    data = read_json(SOURCE_STORY_FILE, required=False)

    if not isinstance(data, dict):
        return None

    status = clean(data.get("status")).lower()
    if status and status not in {"completed", "planned", "in_progress"}:
        return None

    return data


def merge_character_evidence(
    primary_story: dict,
    supplemental_story: dict | None,
    primary_scenes: list[dict],
) -> dict:
    """Add source-story cast records without replacing final AI scenes."""

    combined = dict(primary_story)

    if supplemental_story:
        for key in EXPLICIT_CHARACTER_KEYS:
            primary_value = combined.get(key)
            supplemental_value = supplemental_story.get(key)

            if not primary_value and supplemental_value:
                combined[key] = supplemental_value
            elif isinstance(primary_value, list) and isinstance(
                supplemental_value, list
            ):
                combined[key] = primary_value + supplemental_value

        # Preserve original story-level descriptive records when available.
        for key in ("characters", "character_list", "cast"):
            value = supplemental_story.get(key)
            if isinstance(value, dict):
                value = value.get("characters", [])
            if isinstance(value, list):
                combined.setdefault("_supplemental_" + key, value)

    return combined


def add_supplemental_records(
    found: dict[str, dict],
    story: dict,
) -> None:
    for key in EXPLICIT_CHARACTER_KEYS:
        values = story.get("_supplemental_" + key, [])
        if not isinstance(values, list):
            continue

        for raw in values:
            if not isinstance(raw, dict):
                continue

            name = ""
            for field in NAME_FIELDS:
                name = normalize_name(raw.get(field))
                if name:
                    break

            if not name:
                name = normalize_name(raw.get("name"))

            if not valid_name(name):
                continue

            item = found.setdefault(
                name.casefold(),
                new_character(name),
            )

            for field in DETAIL_FIELDS:
                add_detail(item, field, raw.get(field))

            declared = explicit_gender(raw.get("gender"))
            if declared:
                item["explicit_gender_values"].append(declared)
                item["gender_evidence"].append({
                    "value": declared,
                    "source": "supplemental source story cast record",
                    "confidence": "explicit",
                })

            for alias in normalize_character_values(
                raw.get("aliases", [])
            ):
                if (
                    alias.casefold() != name.casefold()
                    and alias not in item["aliases"]
                ):
                    item["aliases"].append(alias)


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
        "source_fingerprint": file_fingerprint(story),
        "topic": clean(
            get_topic(config)
            or story.get("topic")
            or story.get("title")
        ),
        "title": clean(story.get("title")),
        "format": normalize_format(config),
        "character_count": 0,
        "scene_count": len(scenes),
        "characters": [],
        "scene_character_map": [],
        "world": {},
        "warnings": [
            "CHARACTER_BIBLE is disabled in Input/topic.txt."
        ],
    }


def validate_bible(bible: dict, scenes: list[dict]) -> None:
    if bible.get("status") not in {"completed", "disabled"}:
        raise RuntimeError("Character Bible has an invalid status.")

    characters = bible.get("characters")
    scene_map = bible.get("scene_character_map")

    if not isinstance(characters, list):
        raise RuntimeError("Character Bible characters must be a list.")

    if not isinstance(scene_map, list):
        raise RuntimeError(
            "Character Bible scene_character_map must be a list."
        )

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

        if not isinstance(item.get("visual_identity"), str):
            raise RuntimeError(
                f"Missing visual_identity for {name}."
            )

    expected_keys = {
        (row["part"], row["scene"], row["scene_id"])
        for row in scenes
    }
    actual_keys = set()

    for row in scene_map:
        if not isinstance(row, dict):
            raise RuntimeError("Invalid scene-character map row.")

        key = (
            row.get("part"),
            row.get("scene"),
            row.get("scene_id"),
        )

        if key in actual_keys:
            raise RuntimeError(
                f"Duplicate scene-character mapping: {key}"
            )

        actual_keys.add(key)

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

    if actual_keys != expected_keys:
        missing = expected_keys - actual_keys
        extra = actual_keys - expected_keys
        raise RuntimeError(
            "Scene-character mapping mismatch. "
            f"Missing={len(missing)}, extra={len(extra)}."
        )

    if bible.get("character_count") != len(characters):
        raise RuntimeError("Character count metadata mismatch.")

    if bible.get("scene_count") != len(scenes):
        raise RuntimeError("Scene count metadata mismatch.")


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

    evidence_story = merge_character_evidence(
        story,
        supplemental_story,
        scenes,
    )

    found = aggregate_characters(scenes, evidence_story)
    add_supplemental_records(found, evidence_story)

    # Stable IDs: most frequently mentioned characters first, then name.
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
    }

    warnings = []

    if not characters:
        warnings.append(
            "No reliable character names or explicit character records "
            "were found. No fictional identities were invented."
        )

    if characters and not any(
        item["scene_ids"] for item in characters
    ):
        warnings.append(
            "Cast records were found, but no scene-level appearances "
            "could be confirmed."
        )

    if any(
        item["gender"] == "unspecified"
        for item in characters
    ):
        warnings.append(
            "Some character genders remain unspecified because the source "
            "does not provide unambiguous explicit evidence."
        )

    bible = {
        "status": "completed",
        "version": VERSION,
        "generation_mode": "offline_source_grounded",
        "api_calls": 0,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "source_fingerprint": file_fingerprint(fingerprint_data),
        "topic": topic,
        "title": clean(story.get("title") or topic),
        "format": normalize_format(config),
        "character_count": len(characters),
        "scene_count": len(scenes),
        "characters": characters,
        "scene_character_map": scene_map,
        "world": world,
        "warnings": warnings,
        "limitations": [
            "No external AI calls are made in this stage.",
            "Character names are taken from explicit scene fields, "
            "clear naming phrases and declared cast records.",
            "Visual prompts are not treated as proof of character identity.",
            "Gender is assigned only from explicit source fields.",
            "Unspecified physical traits are not fabricated.",
            "Scene mapping uses exact character names and declared aliases.",
            "Conflicting descriptions remain scene-specific.",
        ],
    }

    validate_bible(bible, scenes)
    return bible


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — SOURCE-GROUNDED CHARACTER BIBLE")
    log("=" * 64)

    input_path = ROOT / "Input" / "topic.txt"
    config = load_input_config(input_path)

    story, source_path = get_story()
    scenes = collect_scenes(story)
    supplemental_story = collect_supplemental_source()

    enabled = config.get("CHARACTER_BIBLE", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {
            "true", "1", "yes", "on", "enabled"
        }
    else:
        enabled = bool(enabled)

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
        return 0

    bible = build_bible(
        config=config,
        story=story,
        source_path=source_path,
        scenes=scenes,
        supplemental_story=supplemental_story,
    )

    write_json(OUTPUT_FILE, bible)

    log("Character Bible created.")
    log("Source:", source_path.relative_to(ROOT))
    log("Scenes examined:", len(scenes))
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
        log("CHARACTER BIBLE FAILED:", exc)
        raise