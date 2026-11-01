#!/usr/bin/env python3
"""Source-grounded character bible for Katha Lok AI photo-motion pipeline."""

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

VERSION = "character-bible-v4-hindi-source-grounded"

DETAIL_FIELDS = (
    "age", "appearance", "clothing", "hair", "eyes",
    "face_features", "skin_tone", "body_features",
    "distinctive_features", "personality", "role", "gender",
)

NAME_FIELDS = (
    "character_name", "character", "person_name",
    "protagonist", "name",
)

ENGLISH_NAME_PATTERN = re.compile(
    r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2}\b"
)

DEVANAGARI_TOKEN = r"[\u0900-\u097F]+"
HINDI_NAME_PATTERNS = (
    re.compile(
        rf"(?:नाम\s*(?:था|थी|है|था कि|थी कि)|"
        rf"जिसका नाम|जिसका नाम था|जिसका नाम था कि)\s*"
        rf"({DEVANAGARI_TOKEN}(?:\s+{DEVANAGARI_TOKEN}){{0,2}})"
    ),
    re.compile(
        rf"(?:श्री|श्रीमती|सुश्री|डॉ\.?|डॉक्टर|मिस्टर|मिस|"
        rf"राजा|रानी|महाराज|महारानी|कप्तान|इंस्पेक्टर)\s+"
        rf"({DEVANAGARI_TOKEN}(?:\s+{DEVANAGARI_TOKEN}){{0,2}})"
    ),
    re.compile(
        rf"\b({DEVANAGARI_TOKEN}(?:\s+{DEVANAGARI_TOKEN}){{0,1}})"
        rf"\s+(?:ने|को|से|पर|की|का|के)\b"
    ),
)

HONORIFICS = {
    "श्री", "श्रीमती", "सुश्री", "डॉ", "डॉक्टर",
    "मिस्टर", "मिस", "राजा", "रानी", "महाराज",
    "महारानी", "कप्तान", "इंस्पेक्टर",
}

GENERIC_NAMES = {
    "the", "this", "that", "when", "then", "after",
    "before", "scene", "part", "hindi", "india",
    "indian", "story", "meanwhile", "finally",
    "suddenly", "however", "because", "chapter",
    "someone", "nobody", "everyone", "what", "where",
    "once", "later", "inside", "outside", "morning",
    "evening", "photorealistic", "cinematic", "natural",
    "realistic", "lighting", "camera", "visual",
    "previous", "next", "character", "context",
    "live", "action",
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

MALE_CUES = (
    "लड़का", "लड़के", "लड़का", "लड़के", "पुरुष",
    "आदमी", "बेटा", "पुत्र", "पिता", "बाप",
    "भाई", "पति", "दूल्हा", "राजा", "महाराजा",
    "सम्राट", "नायक", "युवक", "बोला", "कहता है",
    "male", "man", "boy", "son", "father",
    "brother", "husband", "king", "prince",
    "he/him", "his",
)

FEMALE_CUES = (
    "लड़की", "लड़कियां", "लड़कियाँ", "लड़की",
    "महिला", "औरत", "बेटी", "पुत्री", "माता",
    "माँ", "मां", "बहन", "पत्नी", "दुल्हन",
    "रानी", "महारानी", "युवती", "बोली",
    "कहती है", "female", "woman", "girl",
    "daughter", "mother", "sister", "wife",
    "queen", "princess", "she/her", "her/hers",
)

MALE_EXPLICIT = {
    "male", "man", "boy", "पुरुष", "आदमी",
    "लड़का", "लड़का", "पुरुष पात्र",
}

FEMALE_EXPLICIT = {
    "female", "woman", "girl", "महिला", "औरत",
    "लड़की", "लड़की", "महिला पात्र",
}


def clean(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def log(*items: Any) -> None:
    print(*items, flush=True)


def read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        log("WARNING: Cannot read", path.name, "-", exc)
        return None
    return data if isinstance(data, dict) else None


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


def get_story() -> tuple[dict, Path]:
    errors = []

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


def normalize_character_values(value: Any) -> list[str]:
    result = []

    if isinstance(value, str):
        value = [value]

    if not isinstance(value, list):
        return result

    for item in value:
        if isinstance(item, dict):
            name = ""
            for key in NAME_FIELDS:
                name = clean(item.get(key))
                if name:
                    break
            if not name:
                name = clean(item.get("name"))
        else:
            name = clean(item)

        if name and name not in result:
            result.append(name)

    return result


def collect_scenes(story: dict) -> list[dict]:
    rows = []

    for part_index, part in enumerate(story.get("parts", []), 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part {part_index}")

        scenes = part.get("scenes")
        if not isinstance(scenes, list):
            raise RuntimeError(
                f"Story part {part_index} has no valid scenes list"
            )

        for scene_index, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Invalid scene at part {part_index}, scene {scene_index}"
                )

            scene_id = (
                clean(scene.get("scene_id"))
                or f"part_{part_index:02d}_scene_{scene_index:02d}"
            )

            row = {
                "part": part_index,
                "scene": scene_index,
                "scene_id": scene_id,
                "title": clean(scene.get("title")),
                "narration": clean(
                    scene.get("narration")
                    or scene.get("text")
                    or scene.get("story_text")
                ),
                "dialogue": clean(scene.get("dialogue")),
                "location": clean(
                    scene.get("location") or scene.get("setting")
                ),
                "time_period": clean(scene.get("time_period")),
                "characters": normalize_character_values(
                    scene.get("characters")
                ),
            }

            for field in DETAIL_FIELDS:
                row[field] = clean(scene.get(field))

            for field in NAME_FIELDS:
                row[field] = clean(scene.get(field))

            rows.append(row)

    if not rows:
        raise RuntimeError("Story contains no usable scenes.")

    return rows


def candidate_text(row: dict) -> str:
    return " ".join(
        value for value in (
            clean(row.get("title")),
            clean(row.get("narration")),
            clean(row.get("dialogue")),
        ) if value
    )


def normalize_name(value: Any) -> str:
    name = clean(value).strip(
        ".,:;!?()[]{}'\"“”‘’।॥-–—"
    )
    name = re.sub(r"\s+", " ", name)

    tokens = name.split()
    while tokens and tokens[0] in HONORIFICS:
        tokens.pop(0)

    return " ".join(tokens).strip()


def is_valid_name(name: str) -> bool:
    name = normalize_name(name)
    if not name or len(name) > 80:
        return False

    folded = name.casefold()
    if folded in GENERIC_NAMES:
        return False

    if name in HINDI_STOP_NAMES:
        return False

    if len(name.split()) > 3:
        return False

    if name.isupper():
        return False

    return bool(
        re.search(r"[A-Za-z]", name)
        or re.search(r"[\u0900-\u097F]", name)
    )


def extract_candidates(row: dict) -> list[str]:
    text = candidate_text(row)
    candidates = set()

    # English names are read only from narration, dialogue and title.
    for match in ENGLISH_NAME_PATTERN.findall(text):
        candidates.add(normalize_name(match))

    # Hindi names: use explicit name phrases and grammatical markers.
    for pattern in HINDI_NAME_PATTERNS:
        for match in pattern.findall(text):
            candidates.add(normalize_name(match))

    # Explicit character fields are the strongest source.
    for field in NAME_FIELDS:
        value = normalize_name(row.get(field))
        if value:
            candidates.add(value)

    for value in row.get("characters", []):
        value = normalize_name(value)
        if value:
            candidates.add(value)

    return sorted(
        {
            name for name in candidates
            if is_valid_name(name)
        },
        key=str.casefold,
    )


def local_context(text: str, name: str, radius: int = 180) -> str:
    if not text or not name:
        return ""

    match = re.search(re.escape(name), text, flags=re.IGNORECASE)
    if not match:
        return ""

    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return text[start:end]


def explicit_gender(value: Any) -> str | None:
    value = clean(value).casefold()

    if value in MALE_EXPLICIT:
        return "male"
    if value in FEMALE_EXPLICIT:
        return "female"
    return None


def gender_evidence(row: dict, name: str) -> list[dict]:
    evidence = []

    declared = explicit_gender(row.get("gender"))
    if declared:
        evidence.append({
            "gender": declared,
            "source": "explicit scene gender field",
            "confidence": "explicit",
        })

    context = local_context(candidate_text(row), name)

    if not context:
        context = " ".join(
            clean(row.get(key))
            for key in (
                "character_name", "appearance",
                "clothing", "role", "gender",
            )
        )

    lowered = context.casefold()

    male_hits = [
        cue for cue in MALE_CUES
        if cue.casefold() in lowered
    ]
    female_hits = [
        cue for cue in FEMALE_CUES
        if cue.casefold() in lowered
    ]

    if male_hits and not female_hits:
        evidence.append({
            "gender": "male",
            "source": "nearby story wording",
            "confidence": "heuristic",
            "cues": male_hits[:5],
        })
    elif female_hits and not male_hits:
        evidence.append({
            "gender": "female",
            "source": "nearby story wording",
            "confidence": "heuristic",
            "cues": female_hits[:5],
        })
    elif male_hits and female_hits:
        evidence.append({
            "gender": "conflict",
            "source": "contradictory nearby wording",
            "confidence": "ambiguous",
            "cues": male_hits[:3] + female_hits[:3],
        })

    return evidence

def infer_gender(
    source_rows: list[tuple[dict, str]],
) -> tuple[str, list[dict], str]:
    evidence = []

    for row, name in source_rows:
        evidence.extend(gender_evidence(row, name))

    explicit = {
        item["gender"] for item in evidence
        if item.get("confidence") == "explicit"
    }
    inferred = {
        item["gender"] for item in evidence
        if item.get("confidence") == "heuristic"
    }
    conflict = any(
        item.get("gender") == "conflict"
        for item in evidence
    )

    if len(explicit) == 1 and not conflict:
        gender = next(iter(explicit))
        if inferred and inferred != {gender}:
            return "unspecified", evidence, "conflicting evidence"
        return gender, evidence, "explicit source field"

    if len(explicit) > 1:
        return "unspecified", evidence, "conflicting explicit fields"

    if conflict or len(inferred) > 1:
        return "unspecified", evidence, "ambiguous source wording"

    if len(inferred) == 1:
        return next(iter(inferred)), evidence, "nearby story wording"

    return "unspecified", evidence, "no reliable gender evidence"


def empty_character(name: str) -> dict:
    return {
        "name": name,
        "mention_count": 0,
        "scene_ids": [],
        "source_rows": [],
        "source_details": [],
        "detail_values": {
            field: [] for field in DETAIL_FIELDS
        },
    }


def add_character_detail(item: dict, row: dict, name: str) -> None:
    item["mention_count"] += 1

    scene_id = clean(row.get("scene_id"))
    if scene_id and scene_id not in item["scene_ids"]:
        item["scene_ids"].append(scene_id)

    item["source_rows"].append((row, name))

    for field in DETAIL_FIELDS:
        value = clean(row.get(field))
        if value and value not in item["detail_values"][field]:
            item["detail_values"][field].append(value)

    for field in (
        "age", "appearance", "clothing", "hair", "eyes",
        "face_features", "skin_tone", "body_features",
        "distinctive_features", "personality", "role",
    ):
        value = clean(row.get(field))
        if value:
            detail = f"{field}: {value}"
            if detail not in item["source_details"]:
                item["source_details"].append(detail)


def aggregate_characters(
    scenes: list[dict],
    story: dict,
) -> dict[str, dict]:
    characters = {}

    # Collect names from actual scene narration and explicit scene fields.
    for row in scenes:
        for name in extract_candidates(row):
            key = name.casefold()
            item = characters.setdefault(key, empty_character(name))
            add_character_detail(item, row, name)

    # Also read explicitly declared story-level character records.
    global_values = []

    for key in (
        "characters", "character_list", "cast",
        "main_characters", "character_bible",
    ):
        value = story.get(key)

        if isinstance(value, dict):
            value = value.get("characters", [])

        if isinstance(value, list):
            global_values.extend(value)

    for raw in global_values:
        if isinstance(raw, dict):
            name = ""

            for field in NAME_FIELDS:
                name = normalize_name(raw.get(field))
                if name:
                    break

            if not name:
                name = normalize_name(raw.get("name"))
            if not name:
                continue

            if not is_valid_name(name):
                continue

            key = name.casefold()
            item = characters.setdefault(key, empty_character(name))

            # Story-level descriptions are retained as source evidence.
            for field in DETAIL_FIELDS:
                value = clean(raw.get(field))
                if value and value not in item["detail_values"][field]:
                    item["detail_values"][field].append(value)

            for field in (
                "age", "appearance", "clothing", "hair", "eyes",
                "face_features", "skin_tone", "body_features",
                "distinctive_features", "personality", "role",
            ):
                value = clean(raw.get(field))
                if value:
                    detail = f"{field}: {value}"
                    if detail not in item["source_details"]:
                        item["source_details"].append(detail)

            if raw.get("gender"):
                item["source_rows"].append((
                    {
                        "gender": raw.get("gender"),
                        "narration": "",
                        "dialogue": "",
                        "title": "",
                    },
                    name,
                ))

    return dict(sorted(
        characters.items(),
        key=lambda pair: (
            -pair[1]["mention_count"],
            pair[1]["name"].casefold(),
        ),
    ))


def supported_detail(values: list[str], fallback: str) -> str:
    unique = list(dict.fromkeys(
        clean(value) for value in values if clean(value)
    ))

    if not unique:
        return fallback

    if len(unique) == 1:
        return unique[0]

    return (
        "Scene-specific source variants; use the matching scene detail: "
        + " | ".join(unique[:8])
    )


def build_character(found: dict, index: int) -> dict:
    name = found["name"]

    gender, evidence, reason = infer_gender(
        found["source_rows"]
    )

    values = found["detail_values"]

    age = supported_detail(
        values["age"], "Not specified in source"
    )
    appearance = supported_detail(
        values["appearance"], "Not specified in source"
    )
    clothing = supported_detail(
        values["clothing"],
        "Use explicit scene clothing; do not invent clothing.",
    )
    hair = supported_detail(
        values["hair"], "Not specified in source"
    )
    eyes = supported_detail(
        values["eyes"], "Not specified in source"
    )
    face = supported_detail(
        values["face_features"], "Not specified in source"
    )
    skin = supported_detail(
        values["skin_tone"], "Not specified in source"
    )
    body = supported_detail(
        values["body_features"], "Not specified in source"
    )
    distinctive = supported_detail(
        values["distinctive_features"], "Not specified in source"
    )
    personality = supported_detail(
        values["personality"], "Not specified in source"
    )
    role = supported_detail(
        values["role"], "Follow the actual story scene."
    )

    if gender == "male":
        gender_rule = (
            "Source supports male identity. Preserve it; "
            "do not invent unsupported physical traits."
        )
    elif gender == "female":
        gender_rule = (
            "Source supports female identity. Preserve it; "
            "do not invent unsupported physical traits."
        )
    else:
        gender_rule = (
            "Gender is unspecified or conflicting. "
            "Do not guess from the character name."
        )

    visual_identity = (
        f"Character: {name}. "
        f"Gender: {gender_rule} "
        f"Age: {age}. Appearance: {appearance}. "
        f"Hair: {hair}. Eyes: {eyes}. Face: {face}. "
        f"Skin: {skin}. Body: {body}. Clothing: {clothing}. "
        f"Distinctive features: {distinctive}. "
        "Keep the same recognizable face, age range, hair, "
        "skin tone and body proportions across connected scenes. "
        "Change clothing only when the story explicitly requires it."
    )

    character_id = f"character_{index:03d}"

    return {
        # Both keys are required for compatibility with visual consumers.
        "id": character_id,
        "character_id": character_id,
        "name": name,
        "role": role,
        "importance": (
            "recurring"
            if found["mention_count"] > 1
            else "mentioned"
        ),
        "age": age,
        "gender": gender,
        "gender_evidence": evidence[:30],
        "gender_reason": reason,
        "personality": personality,
        "appearance": appearance,
        "face_features": face,
        "skin_tone": skin,
        "hair": hair,
        "eyes": eyes,
        "body_features": body,
        "clothing": clothing,
        "distinctive_features": distinctive,
        "visual_identity": visual_identity,
        "continuity_notes": (
            "Preserve identity across scenes. Explicit scene-specific "
            "details take priority. Do not combine conflicting "
            "descriptions into one appearance."
        ),
        "consistency_rules": [
            "Do not change face identity between scenes.",
            "Do not infer gender from a name alone.",
            "Do not invent age, skin tone, hair or clothing.",
            "Explicit scene descriptions take priority.",
            "Preserve ambiguity when source evidence is insufficient.",
            "Maintain realistic human anatomy and natural skin texture.",
            "Do not change body proportions or apparent age between scenes.",
            "Do not apply another character's clothing or facial traits.",
        ],
        "mention_count": found["mention_count"],
        "scene_ids": found["scene_ids"],
        "source_details": found["source_details"][:60],
    }


def build_scene_character_map(
    scenes: list[dict],
    characters: list[dict],
) -> list[dict]:
    lookup = {
        item["name"].casefold(): item["id"]
        for item in characters
    }

    result = []

    for row in scenes:
        names = extract_candidates(row)
        ids = []

        for name in names:
            character_id = lookup.get(name.casefold())
            if character_id and character_id not in ids:
                ids.append(character_id)

        result.append({
            "scene_id": row["scene_id"],
            "part": row["part"],
            "scene": row["scene"],
            "character_ids": ids,
            "character_names": names,
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
        "setting": "; ".join(locations[:30]) or "Not specified",
        "time_period": "; ".join(periods[:15]) or "Not specified",
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
            "Avoid anachronistic clothing and architecture.",
            "Do not add events that contradict the story.",
            "Prefer explicit scene descriptions over assumptions.",
            "Keep recurring locations visually recognizable.",
        ],
    }


def build_bible(
    config: dict,
    story: dict,
    source_path: Path,
    scenes: list[dict],
) -> dict:
    topic = clean(
        get_topic(config)
        or story.get("topic")
        or story.get("title")
    )

    found = aggregate_characters(scenes, story)

    characters = [
        build_character(item, index)
        for index, item in enumerate(found.values(), 1)
    ]

    scene_map = build_scene_character_map(scenes, characters)

    world = build_world(scenes)

    source_data = {
        "version": VERSION,
        "story": story,
        "format": normalize_format(config),
        "character_consistency": config.get(
            "CHARACTER_CONSISTENCY", True
        ),
        "world_consistency": config.get(
            "WORLD_CONSISTENCY", True
        ),
    }

    fingerprint = hashlib.sha256(
        json.dumps(
            source_data,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    return {
        "status": "completed",
        "version": VERSION,
        "generation_mode": "offline_source_grounded",
        "api_calls": 0,
        "source_file": source_path.relative_to(ROOT).as_posix(),
        "source_fingerprint": fingerprint,
        "topic": topic,
        "title": clean(story.get("title") or topic),
        "format": normalize_format(config),
        "character_count": len(characters),
        "scene_count": len(scenes),
        "characters": characters,
        "scene_character_map": scene_map,
        "world": world,
        "warnings": (
            [
                "No reliable character names or explicit character records "
                "were found. No fictional identities were invented."
            ]
            if not characters else []
        ),
        "limitations": [
            "Character detection uses narration, dialogue and explicit fields.",
            "Hindi names are detected through explicit naming phrases and "
            "nearby grammatical markers.",
            "Visual prompts are never used as evidence for character names.",
            "Gender is assigned only when source evidence is consistent.",
            "Unspecified physical traits are not fabricated.",
            "This stage makes no external AI calls.",
        ],
    }


def build_disabled_bible(config: dict, story: dict) -> dict:
    return {
        "status": "disabled",
        "version": VERSION,
        "generation_mode": "disabled_by_input",
        "api_calls": 0,
        "topic": clean(
            get_topic(config)
            or story.get("topic")
            or story.get("title")
        ),
        "title": clean(story.get("title")),
        "format": normalize_format(config),
        "character_count": 0,
        "scene_count": 0,
        "characters": [],
        "scene_character_map": [],
        "world": {},
        "warnings": ["CHARACTER_BIBLE is disabled in Input/topic.txt"],
    }


def main() -> int:
    log("=" * 64)
    log("KATHA LOK AI — HINDI SOURCE-GROUNDED CHARACTER BIBLE")
    log("=" * 64)

    config = load_input_config(ROOT / "Input" / "topic.txt")
    story, source_path = get_story()

    enabled = config.get("CHARACTER_BIBLE", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {
            "true", "1", "yes", "on", "enabled"
        }

    if not enabled:
        bible = build_disabled_bible(config, story)
        write_json(OUTPUT_FILE, bible)
        log("Character Bible disabled by Input/topic.txt")
        log("Output:", OUTPUT_FILE.relative_to(ROOT))
        return 0

    scenes = collect_scenes(story)

    bible = build_bible(
        config=config,
        story=story,
        source_path=source_path,
        scenes=scenes,
    )

    write_json(OUTPUT_FILE, bible)

    log("Character Bible created.")
    log("Source:", source_path.relative_to(ROOT))
    log("Scenes examined:", len(scenes))
    log("Characters detected:", bible["character_count"])
    log("Scene-character mappings:", len(bible["scene_character_map"]))
    log("API calls:", bible["api_calls"])
    log("Output:", OUTPUT_FILE.relative_to(ROOT))

    if bible["warnings"]:
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