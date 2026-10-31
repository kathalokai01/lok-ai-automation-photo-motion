#!/usr/bin/env python3
"""Katha Lok AI — source-grounded character bible."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_input_config, normalize_format, get_topic

STORY_DIR = ROOT / "output" / "story"
AI_STORY_FILE = STORY_DIR / "ai_story.json"
SOURCE_STORY_FILE = STORY_DIR / "story.json"
OUTPUT_FILE = STORY_DIR / "character_bible.json"

VERSION = "source-grounded-character-bible-v3-narration-only"

NAME_PATTERN = re.compile(
    r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2}\b"
)

GENERIC_NAMES = {
    "The", "This", "That", "When", "Then", "After", "Before",
    "Scene", "Part", "Hindi", "India", "Indian", "Story",
    "Meanwhile", "Finally", "Suddenly", "However", "Because",
    "Chapter", "Someone", "Nobody", "Everyone", "What", "Where",
    "Once", "Later", "Inside", "Outside", "Morning", "Evening",
    "Photorealistic", "Cinematic", "Natural", "Realistic",
    "Lighting", "Camera", "Visual", "Previous", "Next",
    "Character", "Story", "Context", "Live", "Action",
}

MALE_CUES = (
    "लड़का", "लड़के", "पुरुष", "आदमी", "बेटा", "पुत्र",
    "पिता", "बाप", "भाई", "पति", "दूल्हा", "राजा",
    "महाराजा", "सम्राट", "नायक", "युवक", "बोला", "कहता है",
    "male", "man", "boy", "son", "father", "brother",
    "husband", "king", "prince", "he/him", "his",
)

FEMALE_CUES = (
    "लड़की", "लड़कियां", "लड़कियाँ", "महिला", "औरत",
    "बेटी", "पुत्री", "माता", "माँ", "मां", "बहन",
    "पत्नी", "दुल्हन", "रानी", "महारानी", "युवती",
    "बोली", "कहती है", "female", "woman", "girl",
    "daughter", "mother", "sister", "wife", "queen",
    "princess", "she/her", "her/hers",
)

MALE_EXPLICIT = {
    "male", "man", "boy", "पुरुष", "आदमी", "लड़का",
    "लड़का", "पुरुष पात्र", "लड़के", "लड़के",
}

FEMALE_EXPLICIT = {
    "female", "woman", "girl", "महिला", "औरत",
    "लड़की", "लड़की", "महिला पात्र",
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


def clean(value):
    if isinstance(value, (dict, list, tuple)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def log(*items):
    print(*items, flush=True)


def read_json(path):
    if not path.is_file():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None

    return data if isinstance(data, dict) else None


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
    errors = []

    for path in (AI_STORY_FILE, SOURCE_STORY_FILE):
        data = read_json(path)

        if not isinstance(data, dict):
            errors.append(f"{path.name} missing or invalid")
            continue

        if data.get("status") != "completed":
            errors.append(f"{path.name} is not completed")
            continue

        if not isinstance(data.get("parts"), list) or not data["parts"]:
            errors.append(f"{path.name} has no story parts")
            continue

        return data, path

    raise RuntimeError(
        "No completed story found. " + "; ".join(errors)
    )


def collect_scenes(story):
    rows = []

    for part_index, part in enumerate(story["parts"], 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part: {part_index}")

        scenes = part.get("scenes", [])

        if not isinstance(scenes, list):
            raise RuntimeError(f"Invalid scene list in part {part_index}")

        for scene_index, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Invalid scene record: {part_index}:{scene_index}"
                )

            row = {
                "part": part_index,
                "scene": scene_index,
                "scene_id": clean(scene.get("scene_id"))
                or f"part_{part_index:02d}_scene_{scene_index:02d}",
                "title": clean(scene.get("title")),
                "narration": clean(scene.get("narration")),
                "dialogue": clean(scene.get("dialogue")),
                "location": clean(
                    scene.get("location") or scene.get("setting")
                ),
                "time_period": clean(scene.get("time_period")),
                "characters": [],
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
                row["characters"] = [clean(names)]

            rows.append(row)

    if not rows:
        raise RuntimeError("Completed story contains no usable scenes.")

    return rows


def candidate_text(row):
    """Use story text only; never parse generated visual prompts."""
    return " ".join((
        clean(row.get("title")),
        clean(row.get("narration")),
        clean(row.get("dialogue")),
    ))


def extract_candidates(row):
    candidates = set()

    # Capitalized English names are detected only in story narration/dialogue.
    candidates.update(NAME_PATTERN.findall(candidate_text(row)))

    # Explicit character fields are stronger evidence than heuristics.
    for field in NAME_FIELDS:
        value = clean(row.get(field))
        if value:
            candidates.add(value)

    for value in row.get("characters", []):
        value = clean(value)
        if value:
            candidates.add(value)

    generic = {item.casefold() for item in GENERIC_NAMES}
    result = []

    for name in candidates:
        name = clean(name).strip(".,:;!?()[]{}'\"“”‘’")

        if (
            len(name) < 2
            or len(name) > 80
            or name.casefold() in generic
            or name.isupper()
        ):
            continue

        result.append(name)

    return sorted(set(result), key=str.casefold)


def local_context(text, name, radius=140):
    text = clean(text)
    name = clean(name)

    if not text or not name:
        return ""

    match = re.search(re.escape(name), text, flags=re.IGNORECASE)

    if not match:
        return ""

    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)

    return text[start:end]


def explicit_gender(value):
    value = clean(value).casefold()

    if value in {item.casefold() for item in MALE_EXPLICIT}:
        return "male"

    if value in {item.casefold() for item in FEMALE_EXPLICIT}:
        return "female"

    return None


def gender_evidence(row, name):
    evidence = []

    declared = explicit_gender(row.get("gender"))

    if declared:
        evidence.append({
            "gender": declared,
            "source": "explicit scene gender field",
            "confidence": "explicit",
        })

    context_source = candidate_text(row)
    context = local_context(context_source, name)

    if not context:
        context = " ".join((
            clean(row.get("character_name")),
            clean(row.get("appearance")),
            clean(row.get("clothing")),
            clean(row.get("role")),
            clean(row.get("gender")),
        ))

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


def infer_gender(source_rows):
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


def aggregate_characters(scenes):
    characters = {}

    for row in scenes:
        for name in extract_candidates(row):
            key = name.casefold()

            item = characters.setdefault(key, {
                "name": name,
                "mention_count": 0,
                "scene_ids": [],
                "source_rows": [],
                "source_details": [],
                "detail_values": {
                    field: [] for field in DETAIL_FIELDS
                },
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
    unique = list(dict.fromkeys(
        clean(value) for value in values if clean(value)
    ))

    if not unique:
        return fallback

    if len(unique) == 1:
        return unique[0]

    return (
        "Source variants require scene-level selection: "
        + " | ".join(unique[:5])
    )


def build_character(found, index):
    name = found["name"]

    gender, evidence, reason = infer_gender(
        found["source_rows"]
    )

    values = found["detail_values"]

    age = supported_detail(values["age"], "Not specified in source")
    appearance = supported_detail(
        values["appearance"], "Not specified in source"
    )
    clothing = supported_detail(
        values["clothing"],
        "Use explicit scene clothing; do not invent clothing.",
    )
    hair = supported_detail(values["hair"], "Not specified in source")
    eyes = supported_detail(values["eyes"], "Not specified in source")
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
            "Source supports male identity. Preserve it without adding "
            "unsupported appearance or clothing."
        )
    elif gender == "female":
        gender_rule = (
            "Source supports female identity. Preserve it without adding "
            "unsupported appearance or clothing."
        )
    else:
        gender_rule = (
            "Gender is unspecified or conflicting. Do not guess from name."
        )

    visual_identity = (
        f"Character: {name}. Gender: {gender_rule} "
        f"Age: {age}. Appearance: {appearance}. Hair: {hair}. "
        f"Eyes: {eyes}. Face: {face}. Skin: {skin}. Body: {body}. "
        f"Clothing: {clothing}. Distinctive features: {distinctive}. "
        "Preserve the same recognizable face, age range, hair, skin tone "
        "and body proportions across connected scenes. Change clothing "
        "only when the story explicitly requires it."
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
            "details take priority over general notes. Do not blend "
            "conflicting descriptions."
        ),
        "consistency_rules": [
            "Do not change face identity between scenes.",
            "Do not infer gender from a name alone.",
            "Do not invent age, skin tone, hair or clothing.",
            "Explicit scene descriptions take priority.",
            "Preserve ambiguity when source evidence is insufficient.",
            "Maintain realistic anatomy and natural skin texture.",
        ],
        "mention_count": found["mention_count"],
        "scene_ids": found["scene_ids"],
        "source_details": found["source_details"][:50],
    }


def build_bible(config, story, source_path, scenes):
    topic = clean(
        get_topic(config) or story.get("topic") or story.get("title")
    )

    found = aggregate_characters(scenes)

    characters = [
        build_character(item, index)
        for index, item in enumerate(found, 1)
    ]

    locations = []
    periods = []

    for row in scenes:
        if row["location"] and row["location"] not in locations:
            locations.append(row["location"])

        if row["time_period"] and row["time_period"] not in periods:
            periods.append(row["time_period"])

    world = {
        "setting": "; ".join(locations[:20]) or "Not specified",
        "time_period": "; ".join(periods[:10]) or "Not specified",
        "geography": "Use only locations supported by the story.",
        "architecture": "Match the source scene.",
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
        ],
    }

    source_data = {
        "version": VERSION,
        "story": story,
        "config": {
            "format": normalize_format(config),
            "character_consistency": config.get(
                "CHARACTER_CONSISTENCY", True
            ),
            "world_consistency": config.get(
                "WORLD_CONSISTENCY", True
            ),
        },
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
        "characters": characters,
        "world": world,
        "limitations": [
            "Character detection uses narration, dialogue and explicit fields.",
            "Visual prompts are excluded from name detection.",
            "Hindi names should be supplied in explicit character fields "
            "when they cannot be reliably identified from story text.",
            "Gender is assigned only when source evidence is consistent.",
            "Unspecified physical traits are not fabricated.",
            "This offline stage makes no external AI calls.",
        ],
    }


def main():
    log("=" * 60)
    log("KATHA LOK AI — SOURCE-GROUNDED CHARACTER BIBLE")
    log("=" * 60)

    config = load_input_config()
    story, source_path = get_story()
    scenes = collect_scenes(story)

    bible = build_bible(
        config,
        story,
        source_path,
        scenes,
    )

    write_json(OUTPUT_FILE, bible)

    log("Character bible created.")
    log("Source:", source_path.relative_to(ROOT))
    log("Scenes examined:", len(scenes))
    log("Characters detected:", bible["character_count"])
    log("API calls:", bible["api_calls"])
    log("Output:", OUTPUT_FILE.relative_to(ROOT))
    log("CHARACTER BIBLE: SUCCESS")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"CHARACTER BIBLE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)