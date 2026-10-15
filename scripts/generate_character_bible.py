#!/usr/bin/env python3
"""Build a deterministic, offline character and world bible."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import load_input_config, normalize_format, get_topic, get_story_text

STORY_DIR = ROOT / "output" / "story"
AI_STORY_FILE = STORY_DIR / "ai_story.json"
SOURCE_STORY_FILE = STORY_DIR / "story.json"
OUTPUT_FILE = STORY_DIR / "character_bible.json"
VERSION = "offline-character-bible-v1"

NAME_PATTERN = re.compile(
    r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2}\b"
)

GENERIC_NAMES = {
    "The", "This", "That", "When", "Then", "After", "Before",
    "Scene", "Part", "Hindi", "India", "Indian", "Story",
    "Meanwhile", "Finally", "Suddenly", "However", "Because",
}

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
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)

def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()

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
            rows.append({
                "part": part_index,
                "scene": scene_index,
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
                "clothing": clean(scene.get("clothing")),
                "age": clean(scene.get("age")),
                "appearance": clean(scene.get("appearance")),
                "character_name": clean(scene.get("character_name")),
            })
    if not rows:
        raise RuntimeError("Completed story contains no usable scenes.")
    return rows

def find_character_mentions(scenes):
    mentions = {}
    for row in scenes:
        text = " ".join((
            row["title"], row["narration"], row["dialogue"],
            row["visual"], row["character_name"],
        ))
        candidates = set(NAME_PATTERN.findall(text))
        explicit = row["character_name"]
        if explicit:
            candidates.add(explicit)

        for name in candidates:
            name = clean(name)
            if (
                len(name) < 3
                or name in GENERIC_NAMES
                or name.isupper()
            ):
                continue
            item = mentions.setdefault(name, {
                "name": name,
                "mention_count": 0,
                "scene_ids": [],
                "source_details": [],
            })
            item["mention_count"] += 1
            scene_id = f"part_{row['part']:02d}_scene_{row['scene']:02d}"
            if scene_id not in item["scene_ids"]:
                item["scene_ids"].append(scene_id)

            details = []
            for key in ("age", "appearance", "clothing"):
                if row[key] and row[key] not in item["source_details"]:
                    details.append(f"{key}: {row[key]}")
            item["source_details"].extend(details)

    return sorted(
        mentions.values(),
        key=lambda item: (-item["mention_count"], item["name"].casefold()),
    )

def build_bible(config, story, source_path, scenes):
    topic = clean(get_topic(config) or story.get("topic") or story.get("title"))
    story_text = clean(get_story_text(config))
    characters = []

    for index, found in enumerate(find_character_mentions(scenes), 1):
        name = found["name"]
        characters.append({
            "character_id": f"character_{index:03d}",
            "name": name,
            "role": "Unverified story character; confirm role from source.",
            "importance": (
                "recurring" if found["mention_count"] > 1 else "mentioned"
            ),
            "age": "Not specified in source",
            "gender": "Not inferred",
            "personality": "Not specified in source",
            "appearance": "Not specified in source",
            "face_features": "Do not invent; preserve source reference if available.",
            "skin_tone": "Not specified in source",
            "hair": "Not specified in source",
            "eyes": "Not specified in source",
            "body_features": "Not specified in source",
            "clothing": "Use scene-specific clothing only when explicitly provided.",
            "distinctive_features": "Not specified in source",
            "visual_identity": (
                f"Keep the identity associated with the name '{name}' "
                "consistent wherever the source identifies this character."
            ),
            "continuity_notes": (
                "This entry is extracted automatically; details not stated "
                "in the story remain unspecified."
            ),
            "consistency_rules": [
                "Do not change established identity between scenes.",
                "Do not invent age, ethnicity, clothing or facial features.",
                "Use explicit scene details when present.",
                "Keep historical setting and visual details plausible.",
            ],
            "mention_count": found["mention_count"],
            "scene_ids": found["scene_ids"],
            "source_details": found["source_details"],
        })

    locations = []
    periods = []
    visual_context = []
    for row in scenes:
        if row["location"] and row["location"] not in locations:
            locations.append(row["location"])
        if row["time_period"] and row["time_period"] not in periods:
            periods.append(row["time_period"])
        if row["visual"]:
            visual_context.append(row["visual"])

    world = {
        "setting": "; ".join(locations[:20]) or "Not explicitly specified",
        "time_period": "; ".join(periods[:10]) or "Not explicitly specified",
        "geography": "Use only locations supported by the story.",
        "architecture": "Match the source scene; do not invent a specific era.",
        "environment": "Follow scene descriptions and established continuity.",
        "weather_style": "Use explicit source details; otherwise leave unspecified.",
        "color_and_lighting": "Natural cinematic lighting, consistent with the scene.",
        "continuity_rules": [
            "Keep locations, time of day and weather consistent when scenes connect.",
            "Avoid anachronistic clothing, architecture and objects.",
            "Do not add visual events that contradict the story.",
            "Prefer explicit scene descriptions over generic assumptions.",
        ],
    }

    source = {
        "version": VERSION,
        "topic": topic,
        "story": story,
        "config": {
            "format": normalize_format(config),
            "character_consistency": config.get("CHARACTER_CONSISTENCY", True),
            "world_consistency": config.get("WORLD_CONSISTENCY", True),
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            source, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
    ).hexdigest()

    return {
        "status": "completed",
        "version": VERSION,
        "generation_mode": "offline",
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
            "Character names are heuristic candidates, not verified entities.",
            "Unspecified physical traits are intentionally not fabricated.",
            "This offline step does not call an AI model to infer missing details.",
        ],
    }

def main():
    config = load_input_config()
    story, source_path = get_story()
    scenes = collect_scenes(story)
    bible = build_bible(config, story, source_path, scenes)
    write_json(OUTPUT_FILE, bible)

    print("Offline character bible created.")
    print(f"Characters detected: {bible['character_count']}")
    print(f"Scenes examined: {len(scenes)}")
    print(f"API calls: {bible['api_calls']}")
    print(f"Output: {OUTPUT_FILE.relative_to(ROOT)}")

if __name__ == "__main__":
    main()