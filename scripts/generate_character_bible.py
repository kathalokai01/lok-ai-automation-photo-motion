
#!/usr/bin/env python3
"""Build a source-grounded character bible with conservative gender detection
and consistent visual identity rules. No external API calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_input_config,
    normalize_format,
    get_topic,
    get_story_text,
)

STORY_DIR = ROOT / "output" / "story"
AI_STORY_FILE = STORY_DIR / "ai_story.json"
SOURCE_STORY_FILE = STORY_DIR / "story.json"
OUTPUT_FILE = STORY_DIR / "character_bible.json"

VERSION = "source-grounded-character-bible-v2"

# English names can be detected by capitalization. Hindi/Devanagari names
# should preferably arrive through explicit scene character fields.
NAME_PATTERN = re.compile(
    r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2}\b"
)

GENERIC_NAMES = {
    "The", "This", "That", "When", "Then", "After", "Before",
    "Scene", "Part", "Hindi", "India", "Indian", "Story",
    "Meanwhile", "Finally", "Suddenly", "However", "Because",
    "Chapter", "Someone", "Nobody", "Everyone", "What", "Where",
    "Once", "Later", "Inside", "Outside", "Morning", "Evening",
}

# These cues are intentionally conservative. Ambiguous pronouns such as
# "वह", "उसने", "they", and "you" are not treated as gender evidence.
MALE_CUES = (
    "लड़का", "लड़के", "पुरुष", "आदमी", "बेटा", "पुत्र",
    "पिता", "बाप", "भाई", "पति", "दूल्हा", "राजा",
    "महाराजा", "सम्राट", "नायक", "वृद्ध पुरुष", "युवक",
    "लड़का था", "गया था", "कर रहा था", "बोला", "कहता है",
    "male", "man", "boy", "son", "father", "brother", "husband",
    "king", "prince", "gentleman", "he/him", "his",
)

FEMALE_CUES = (
    "लड़की", "लड़कियां", "लड़कियाँ", "महिला", "औरत",
    "बेटी", "पुत्री", "माता", "माँ", "मां", "बहन",
    "पत्नी", "दुल्हन", "रानी", "महारानी", "नायिका",
    "युवती", "वृद्ध महिला", "गई थी", "कर रही थी",
    "बोली", "कहती है", "female", "woman", "girl", "daughter",
    "mother", "sister", "wife", "queen", "princess",
    "she/her", "her/hers",
)

MALE_EXPLICIT_FIELDS = {
    "male", "man", "boy", "पुरुष", "आदमी", "लड़का", "लड़का",
    "पुरुष पात्र", "लड़के", "लड़के",
}
FEMALE_EXPLICIT_FIELDS = {
    "female", "woman", "girl", "महिला", "औरत", "लड़की", "लड़की",
    "महिला पात्र", "लड़कियां", "लड़कियाँ",
}

DETAIL_FIELDS = (
    "age",
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
    "gender",
)

NAME_FIELDS = (
    "character_name",
    "character",
    "person_name",
    "protagonist",
)

def log(*items):
    print(*items, flush=True)


def clean(value):
    if isinstance(value, (dict, list, tuple)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def read_json(path):
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def get_story():
    for path in (AI_STORY_FILE, SOURCE_STORY_FILE):
        data = read_json(path)
        if (
            isinstance(data, dict)
            and data.get("status") == "completed"
            and isinstance(data.get("parts"), list)
            and data["parts"]
        ):
            return data, path

    raise RuntimeError(
        "No completed story found. Generate story.json before "
        "building the character bible."
    )


def collect_scenes(story):
    rows = []

    for part_index, part in enumerate(story["parts"], 1):
        if not isinstance(part, dict):
            continue

        scenes = part.get("scenes", [])
        if not isinstance(scenes, list):
            continue

        for scene_index, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                continue

            row = {
                "part": part_index,
                "scene": scene_index,
                "scene_id": clean(scene.get("scene_id"))
                or f"part_{part_index:02d}_scene_{scene_index:02d}",
                "title": clean(scene.get("title")),
                "narration": clean(scene.get("narration")),
                "dialogue": clean(scene.get("dialogue")),
                "visual": clean(
                    scene.get("visual_prompt")
                    or scene.get("visual")
                    or scene.get("image_prompt")
                ),
                "location": clean(
                    scene.get("location") or scene.get("setting")
                ),
                "time_period": clean(scene.get("time_period")),
            }

            for key in DETAIL_FIELDS:
                row[key] = clean(scene.get(key))

            for key in NAME_FIELDS:
                row[key] = clean(scene.get(key))

            names = scene.get("characters")
            if isinstance(names, list):
                row["characters"] = [
                    clean(item) for item in names if clean(item)
                ]
            elif isinstance(names, str) and clean(names):
                # A single string is safe to treat as one declared name.
                row["characters"] = [clean(names)]
            else:
                row["characters"] = []

            rows.append(row)

    if not rows:
        raise RuntimeError("Completed story contains no usable scenes.")

    return rows


def scene_text(row):
    fields = (
        "title", "narration", "dialogue", "visual",
        "appearance", "clothing", "role", "gender",
    )
    return " ".join(clean(row.get(key)) for key in fields)


def extract_candidates(row):
    text = scene_text(row)
    candidates = set(NAME_PATTERN.findall(text))

    for field in NAME_FIELDS:
        value = clean(row.get(field))
        if value:
            candidates.add(value)

    for value in row.get("characters", []):
        value = clean(value)
        if value:
            candidates.add(value)

    result = []
    for name in candidates:
        name = clean(name).strip(".,:;!?()[]{}'\"“”‘’")
        if (
            len(name) < 2
            or name.casefold() in {x.casefold() for x in GENERIC_NAMES}
            or name.isupper()
            or len(name) > 80
        ):
            continue
        result.append(name)

    return sorted(set(result), key=str.casefold)


def local_context(text, name, radius=100):
    """Return only a small context around this character's name."""
    text = clean(text)
    name = clean(name)

    if not text or not name:
        return text[:200]

    match = re.search(re.escape(name), text, flags=re.IGNORECASE)
    if not match:
        return ""

    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return text[start:end]


def explicit_gender(value):
    value = clean(value).casefold()
    if not value:
        return None

    if value in {x.casefold() for x in MALE_EXPLICIT_FIELDS}:
        return "male"
    if value in {x.casefold() for x in FEMALE_EXPLICIT_FIELDS}:
        return "female"

    if value in {"man", "male", "boy", "masculine"}:
        return "male"
    if value in {"woman", "female", "girl", "feminine"}:
        return "female"

    return None


def gender_evidence(row, name):
    evidence = []
    field_gender = explicit_gender(row.get("gender"))

    if field_gender:
        evidence.append({
            "gender": field_gender,
            "source": "explicit scene gender field",
            "confidence": "explicit",
        })

    # Look for gendered terms close to the character name. This avoids
    # assigning the gender of another character mentioned elsewhere.
    text = " ".join((
        clean(row.get("title")),
        clean(row.get("narration")),
        clean(row.get("dialogue")),
        clean(row.get("visual")),
        clean(row.get("appearance")),
        clean(row.get("clothing")),
        clean(row.get("role")),
    ))

    context = local_context(text, name)
    if not context:
        context = " ".join((
            clean(row.get("character_name")),
            clean(row.get("appearance")),
            clean(row.get("clothing")),
            clean(row.get("role")),
            clean(row.get("gender")),
        ))

    lowered = context.casefold()
    male_hits = [cue for cue in MALE_CUES if cue.casefold() in lowered]
    female_hits = [cue for cue in FEMALE_CUES if cue.casefold() in lowered]

    # Do not let a generic gender cue override an explicitly contradictory
    # field. Conflicts are reported for review instead.
    if male_hits and not female_hits:
        evidence.append({
            "gender": "male",
            "source": "nearby gendered story wording",
            "confidence": "heuristic",
            "cues": male_hits[:5],
        })
    elif female_hits and not male_hits:
        evidence.append({
            "gender": "female",
            "source": "nearby gendered story wording",
            "confidence": "heuristic",
            "cues": female_hits[:5],
        })
    elif male_hits and female_hits:
        evidence.append({
            "gender": "conflict",
            "source": "contradictory nearby gender cues",
            "confidence": "ambiguous",
            "cues": (male_hits[:3] + female_hits[:3]),
        })

    return evidence


def infer_gender(rows):
    """Return gender only when evidence agrees; never guess from a name."""
    evidence = []
    for row, name in rows:
        evidence.extend(gender_evidence(row, name))

    explicit = {
        item["gender"] for item in evidence
        if item.get("confidence") == "explicit"
    }
    inferred = {
        item["gender"] for item in evidence
        if item.get("confidence") == "heuristic"
    }
    has_conflict = any(
        item.get("gender") == "conflict" for item in evidence
    )

    if len(explicit) == 1 and not has_conflict:
        gender = next(iter(explicit))
        if inferred and inferred != {gender}:
            return "unspecified", evidence, "conflicting source evidence"
        return gender, evidence, "explicit source field"

    if len(explicit) > 1:
        return "unspecified", evidence, "conflicting explicit fields"

    if has_conflict or len(inferred) > 1:
        return "unspecified", evidence, "conflicting or ambiguous wording"

    if len(inferred) == 1:
        return next(iter(inferred)), evidence, "nearby gendered wording"

    return "unspecified", evidence, "no reliable gender evidence"


def aggregate_characters(scenes):
    characters = {}

    for row in scenes:
        candidates = extract_candidates(row)

        for name in candidates:
            key = name.casefold()
            item = characters.setdefault(key, {
                "name": name,
                "mention_count": 0,
                "scene_ids": [],
                "source_details": [],
                "source_rows": [],
                "detail_values": {field: [] for field in DETAIL_FIELDS},
            })

            item["mention_count"] += 1
            if row["scene_id"] not in item["scene_ids"]:
                item["scene_ids"].append(row["scene_id"])

            item["source_rows"].append((row, name))

            for field in DETAIL_FIELDS:
                value = clean(row.get(field))
                if value and value not in item["detail_values"][field]:
                    item["detail_values"][field].append(value)

            for field in (
                "age", "appearance", "clothing", "hair", "eyes",
                "face_features", "skin_tone", "body_features",
                "distinctive_features", "role",
            ):
                value = clean(row.get(field))
                if value:
                    detail = f"{field}: {value}"
                    if detail not in item["source_details"]:
                        item["source_details"].append(detail)

    return sorted(
        characters.values(),
        key=lambda item: (
            -item["mention_count"],
            item["name"].casefold(),
        ),
    )


def supported_detail(values, fallback):
    values = [clean(value) for value in values if clean(value)]
    unique = list(dict.fromkeys(values))

    if not unique:
        return fallback

    if len(unique) == 1:
        return unique[0]

    # Preserve conflicting source statements rather than silently choosing
    # one and accidentally changing the character between scenes.
    return "Source variants require scene-level selection: " + " | ".join(
        unique[:5]
    )


def build_character(found, index):
    name = found["name"]
    gender, evidence, gender_reason = infer_gender(found["source_rows"])
    values = found["detail_values"]

    age = supported_detail(values["age"], "Not specified in source")
    appearance = supported_detail(
        values["appearance"], "Not specified in source"
    )
    clothing = supported_detail(
        values["clothing"],
        "Use the scene's explicit clothing; otherwise keep clothing neutral "
        "and do not invent ceremonial or gender-specific clothing.",
    )
    hair = supported_detail(values["hair"], "Not specified in source")
    eyes = supported_detail(values["eyes"], "Not specified in source")
    face_features = supported_detail(
        values["face_features"], "Not specified in source"
    )
    skin_tone = supported_detail(
        values["skin_tone"], "Not specified in source"
    )
    body_features = supported_detail(
        values["body_features"], "Not specified in source"
    )
    distinctive = supported_detail(
        values["distinctive_features"], "Not specified in source"
    )
    personality = supported_detail(
        values["personality"], "Not specified in source"
    )
    role = supported_detail(
        values["role"],
        "Not verified; follow the actual story scene.",
    )

    if gender == "male":
        gender_rule = (
            "Character is explicitly supported as male by source evidence. "
            "Render as male; do not add sari, sindoor, earrings or other "
            "gender-coded styling unless the story explicitly requires it."
        )
    elif gender == "female":
        gender_rule = (
            "Character is explicitly supported as female by source evidence. "
            "Render as female; do not add beard or moustache unless the "
            "story explicitly requires it."
        )
    else:
        gender_rule = (
            "Gender is unspecified or conflicting in the source. Do not "
            "guess gender from the name. Follow explicit scene descriptions "
            "and avoid adding gender-specific clothing or facial traits."
        )

    visual_identity = (
        f"Recurring character name: {name}. "
        f"Gender constraint: {gender_rule} "
        f"Age: {age}. Appearance: {appearance}. Hair: {hair}. "
        f"Eyes: {eyes}. Face features: {face_features}. "
        f"Skin tone: {skin_tone}. Body features: {body_features}. "
        f"Clothing: {clothing}. Distinctive features: {distinctive}. "
        "Across scenes, preserve the same recognizable face, approximate "
        "age, hair, skin tone and body proportions. Change clothing only "
        "when the story or scene explicitly changes it. Never substitute "
        "a different character because the scene, lighting or camera angle changes."
    )

    return {
        "character_id": f"character_{index:03d}",
        "name": name,
        "role": role,
        "importance": (
            "recurring" if found["mention_count"] > 1 else "mentioned"
        ),
        "age": age,
        "gender": gender,
        "gender_evidence": evidence[:20],
        "gender_reason": gender_reason,
        "personality": personality,
        "appearance": appearance,
        "face_features": face_features,
        "skin_tone": skin_tone,
        "hair": hair,
        "eyes": eyes,
        "body_features": body_features,
        "clothing": clothing,
        "distinctive_features": distinctive,
        "visual_identity": visual_identity,
        "continuity_notes": (
            "Use the same character identity in every scene where this name "
            "refers to the same person. Respect explicit source details. "
            "If source details conflict, use the applicable scene-specific "
            "description instead of blending incompatible appearances."
        ),
        "consistency_rules": [
            "Do not change face identity between scenes.",
            "Do not infer gender from a name alone.",
            "Do not invent age, ethnicity, skin tone, hair, facial hair or clothing.",
            "Do not add sari, sindoor, earrings, moustache or beard unless supported by the source.",
            "Explicit scene-specific details take priority over general notes.",
            "When the source is ambiguous, preserve ambiguity rather than guessing.",
            "Keep realistic anatomy, natural skin texture and believable proportions.",
        ],
        "mention_count": found["mention_count"],
        "scene_ids": found["scene_ids"],
        "source_details": found["source_details"][:50],
    }


def build_bible(config, story, source_path, scenes):
    topic = clean(
        get_topic(config) or story.get("topic") or story.get("title")
    )

    found_characters = aggregate_characters(scenes)
    characters = [
        build_character(found, index)
        for index, found in enumerate(found_characters, 1)
    ]

    locations = []
    periods = []

    for row in scenes:
        if row["location"] and row["location"] not in locations:
            locations.append(row["location"])
        if row["time_period"] and row["time_period"] not in periods:
            periods.append(row["time_period"])

    world = {
        "setting": "; ".join(locations[:20]) or "Not explicitly specified",
        "time_period": "; ".join(periods[:10]) or "Not explicitly specified",
        "geography": "Use only locations supported by the story.",
        "architecture": (
            "Match the source scene; do not invent a specific historical era."
        ),
        "environment": (
            "Follow scene descriptions and established continuity."
        ),
        "weather_style": (
            "Use explicit source details; otherwise leave unspecified."
        ),
        "color_and_lighting": (
            "Natural cinematic lighting, moderate contrast, realistic skin "
            "tones and consistent scene atmosphere."
        ),
        "continuity_rules": [
            "Keep locations, time of day and weather consistent when scenes connect.",
            "Avoid anachronistic clothing, architecture and objects.",
            "Do not add visual events that contradict the story.",
            "Prefer explicit scene descriptions over generic assumptions.",
            "Avoid excessive contrast, oversaturated skin and unnatural shadows.",
        ],
    }

    source = {
        "version": VERSION,
        "topic": topic,
        "story": story,
        "config": {
            "format": normalize_format(config),
            "character_consistency": config.get(
                "CHARACTER_CONSISTENCY", True
            ),
            "world_consistency": config.get("WORLD_CONSISTENCY", True),
        },
    }

    fingerprint = hashlib.sha256(
        json.dumps(
            source,
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
        "characters": characters,
        "world": world,
        "limitations": [
            "Character detection is heuristic; explicitly named scene fields are most reliable.",
            "Gender is only set when source evidence is sufficiently consistent.",
            "Unspecified physical traits are intentionally not fabricated.",
            "This offline step does not call an AI model to infer missing details.",
            "The image model may still occasionally ignore prompt constraints; inspect generated images.",
        ],
    }


def main():
    config = load_input_config()
    story, source_path = get_story()
    scenes = collect_scenes(story)
    bible = build_bible(config, story, source_path, scenes)
    write_json(OUTPUT_FILE, bible)

    print("Source-grounded character bible created.")
    print(f"Characters detected: {bible['character_count']}")
    print(f"Scenes examined: {len(scenes)}")
    print(f"API calls: {bible['api_calls']}")
    print(f"Output: {OUTPUT_FILE.relative_to(ROOT)}")

    unknown_gender = sum(
        row.get("gender") == "unspecified"
        for row in bible["characters"]
    )
    print(f"Characters with unspecified gender: {unknown_gender}")
    print("Continuity rules: enabled")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Character bible generation cancelled.")
    except Exception as exc:
        print(f"CHARACTER BIBLE FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
