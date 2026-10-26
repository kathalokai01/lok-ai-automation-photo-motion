
#!/usr/bin/env python3
"""Katha Lok AI story-to-scene mapper with strict schema validation."""

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
    load_and_validate,
    get_format,
    get_parts,
    get_scenes,
    get_topic,
    get_story_text,
)

STORY_DIR = ROOT / "output" / "story"
SOURCE_FILE = STORY_DIR / "story.json"
PLAN_FILE = STORY_DIR / "story_plan.json"
OUTPUT_FILE = STORY_DIR / "ai_story.json"

VERSION = "offline-ai-story-mapper-v4-short-validation"
MAPPING_VERSION = 5
SHORT_MIN_WORDS = 120
SHORT_MAX_WORDS = 150

SCENE_FIELDS = (
    "narration",
    "dialogue",
    "visual_prompt",
    "negative_prompt",
    "camera_prompt",
    "lighting_prompt",
    "sfx_prompt",
    "music_prompt",
    "suspense_prompt",
)


def log(*values):
    print(*values, flush=True)


def clean(value):
    if isinstance(value, (dict, list, tuple)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def word_count(value):
    return len(re.findall(r"\S+", clean(value)))


def fingerprint(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path, required=False):
    path = Path(path)
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None

    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Cannot read {path}: {exc}") from exc
        return None

    if not isinstance(value, dict):
        if required:
            raise RuntimeError(f"Expected a JSON object in {path}")
        return None
    return value


def save_json(data):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_FILE.with_suffix(".json.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, OUTPUT_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def bool_value(config, key, default=True):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"true", "yes", "on", "1", "enabled"}:
        return True
    if value in {"false", "no", "off", "0", "disabled", ""}:
        return False
    return default


def text_value(item, *keys, default=""):
    if not isinstance(item, dict):
        return default
    for key in keys:
        value = clean(item.get(key))
        if value:
            return value
    return default


def source_visual(scene):
    return text_value(
        scene, "visual", "visual_prompt", "image_prompt",
        "scene_prompt", "visual_description", "description"
    )


def validate_plan(plan, mode, parts_expected, scenes_expected):
    if not isinstance(plan, dict):
        raise RuntimeError("story_plan.json missing or invalid")

    if clean(plan.get("status")).lower() != "planned":
        raise RuntimeError("story_plan.json status must be planned")

    if clean(plan.get("format")).lower() != mode:
        raise RuntimeError("Story plan FORMAT does not match input")

    if int(plan.get("parts_count", -1)) != parts_expected:
        raise RuntimeError("Story plan PARTS mismatch")

    if int(plan.get("scenes_per_part", -1)) != scenes_expected:
        raise RuntimeError("Story plan SCENES mismatch")

    expected_total = parts_expected * scenes_expected
    if int(plan.get("total_scenes", -1)) != expected_total:
        raise RuntimeError("Story plan total_scenes mismatch")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_expected:
        raise RuntimeError("Story plan parts list mismatch")

    global_number = 0
    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != p:
            raise RuntimeError(f"Story plan part numbering mismatch: {p}")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_expected:
            raise RuntimeError(f"Story plan part {p} scene count mismatch")

        for s, scene in enumerate(scenes, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid planned scene {p}:{s}")

            expected_id = f"part_{p:02d}_scene_{s:02d}"
            if clean(scene.get("scene_id")) != expected_id:
                raise RuntimeError(f"Planned scene ID mismatch: {expected_id}")

            if int(scene.get("scene", -1)) != s:
                raise RuntimeError(f"Planned scene number mismatch: {expected_id}")

            if int(scene.get("global_scene_number", -1)) != global_number:
                raise RuntimeError(f"Planned global scene number mismatch: {expected_id}")

    return plan


def validate_source(story, mode, parts_expected, scenes_expected):
    if story.get("status") != "completed":
        raise RuntimeError("story.json is not marked completed")

    if clean(story.get("format")).lower() != mode:
        raise RuntimeError("story.json FORMAT mismatch; regenerate the story first")

    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_expected:
        actual = len(parts) if isinstance(parts, list) else "invalid"
        raise RuntimeError(f"story.json has {actual} parts; expected {parts_expected}")

    total_scenes = parts_expected * scenes_expected
    global_number = 0
    part_counts = []

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != p:
            raise RuntimeError(f"Source part numbering mismatch: {p}")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_expected:
            actual = len(scenes) if isinstance(scenes, list) else 0
            raise RuntimeError(
                f"Part {p} has {actual} scenes; expected {scenes_expected}"
            )

        count = 0
        for s, scene in enumerate(scenes, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid source scene {p}:{s}")

            expected_id = f"part_{p:02d}_scene_{s:02d}"
            if int(scene.get("scene", -1)) != s:
                raise RuntimeError(f"Source scene number mismatch: {expected_id}")

            source_id = clean(scene.get("scene_id"))
            if source_id and source_id != expected_id:
                raise RuntimeError(f"Source scene ID mismatch: {expected_id}")

            narration = text_value(scene, "narration", "text", "dialogue")
            visual = source_visual(scene)
            if not narration:
                raise RuntimeError(f"Empty narration: {expected_id}")
            if not visual:
                raise RuntimeError(f"Empty visual description: {expected_id}")

            count += word_count(narration)

        part_counts.append(count)

    if global_number != total_scenes:
        raise RuntimeError("Source total scene count mismatch")

    if not clean(story.get("hook")):
        raise RuntimeError("story.json has no opening hook")

    if mode == "short":
        for p, amount in enumerate(part_counts, 1):
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"SHORT WORD COUNT ERROR: part {p} has {amount} words; "
                    f"required {SHORT_MIN_WORDS}-{SHORT_MAX_WORDS}. "
                    "Fix story.json before continuing."
                )

    return total_scenes, part_counts


def identity_details(scene):
    labels = (
        ("character name", ("character_name", "person_name")),
        ("gender", ("gender",)),
        ("age", ("age",)),
        ("appearance", ("appearance",)),
        ("clothing", ("clothing",)),
        ("hair", ("hair",)),
        ("distinctive features", ("distinctive_features",)),
    )
    details = []
    for label, keys in labels:
        value = text_value(scene, *keys)
        if value:
            details.append(f"{label}: {value}")

    characters = scene.get("characters")
    if isinstance(characters, list):
        names = [clean(x) for x in characters if clean(x)]
        if names:
            details.append("named characters: " + ", ".join(names))
    elif isinstance(characters, str) and clean(characters):
        details.append("named characters: " + clean(characters))

    return "; ".join(details)


def make_scene(
    source, previous_source, next_source, part_number,
    scene_number, global_number, config, title
):
    scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
    narration = text_value(source, "narration", "text", "dialogue")
    visual = source_visual(source)
    dialogue = text_value(source, "dialogue")
    suspense = text_value(source, "suspense", "suspense_prompt")

    realism = clean(config.get("REALISM", "high")) or "high"
    style = clean(config.get("VISUAL_STYLE", "cinematic_realistic"))
    camera = clean(config.get("CAMERA_STYLE", "cinematic"))
    lighting = clean(config.get("LIGHTING", "cinematic"))
    mood = clean(config.get("MOOD", "dramatic"))

    context = [f"Story title/context: {title}"]
    for label, keys in (
        ("location", ("location", "setting", "world_context")),
        ("time period", ("time_period", "time", "era")),
    ):
        value = text_value(source, *keys)
        if value:
            context.append(f"{label}: {value}")

    identity = identity_details(source)
    if identity:
        context.append("Explicit character details: " + identity)

    if previous_source:
        previous_identity = identity_details(previous_source)
        previous_visual = source_visual(previous_source)
        if previous_identity:
            context.append(
                "Continuity: preserve these explicit details when applicable: "
                + previous_identity
            )
        if previous_visual:
            context.append("Previous scene reference: " + previous_visual[:220])

    if next_source:
        next_visual = source_visual(next_source)
        if next_visual:
            context.append("Next-scene continuity reference: " + next_visual[:180])

    context_text = " ".join(context)

    visual_prompt = (
        f"Story scene: {visual}. Narrative context: {narration[:500]}. "
        f"{context_text} Visual style: {style}; realism: {realism}. "
        "Photorealistic live-action cinematic photography, believable anatomy, "
        "natural skin texture, realistic materials and physically plausible "
        "lighting. Preserve explicit character face, age, clothing and identity "
        "between connected scenes. Never infer or change gender or identity. "
        "Show one coherent moment, not a collage or slideshow. No text, "
        "captions, logos or watermarks."
    )

    camera_prompt = (
        f"{camera} camera language suited to the action. Use establishing "
        "wide shots for locations, medium framing for interactions and "
        "close-ups for important expressions or clues. Vary framing with "
        "purpose. Keep camera movement physically plausible; avoid arbitrary "
        "angles and movement that contradicts the action."
    )

    lighting_prompt = (
        f"{lighting} lighting appropriate to the scene location, time and "
        f"mood ({mood}). Preserve natural skin tones, shadow detail and "
        "highlight detail. Avoid crushed blacks, blown highlights, excessive "
        "contrast and oversaturation unless explicitly required by the scene."
    )

    negatives = [
        "cartoon", "comic illustration", "anime", "slideshow", "collage",
        "unintended text", "embedded subtitles", "watermark", "logo",
        "distorted anatomy", "duplicate people", "inconsistent character identity",
        "unsupported gender or age change", "excessive contrast",
        "blown highlights", "crushed shadows",
    ]
    configured_negative = clean(config.get("NEGATIVE_PROMPT"))
    if configured_negative:
        negatives.append(configured_negative)
    if bool_value(config, "AVOID_NEON", True):
        negatives.append("unmotivated neon lighting")
    if bool_value(config, "AVOID_GLITCH_EFFECTS", True):
        negatives.append("glitch effects")

    sfx_prompt = (
        f"Scene-relevant sound effects for: {visual[:300]}. "
        "Align effects with visible actions. Use silence when appropriate. "
        "Avoid unrelated impacts, unrequested voices and repetitive stingers."
    )

    music_prompt = (
        f"{mood} cinematic background music appropriate to this scene. "
        "Maintain continuity between connected scenes. Keep music beneath "
        "narration; avoid abrupt volume changes, lyrics and intelligible speech. "
        "Do not substitute music for scene-specific sound effects."
    )

    return {
        "part": part_number,
        "scene": scene_number,
        "part_id": f"part_{part_number:02d}",
        "scene_id": scene_id,
        "global_scene_number": global_number,
        "mapping_version": MAPPING_VERSION,
        "source_scene_id": clean(source.get("scene_id")) or scene_id,
        "role": text_value(source, "purpose", "role", "scene_purpose"),
        "status": "completed",
        "narration": narration,
        "dialogue": dialogue,
        "visual_prompt": visual_prompt,
        "negative_prompt": ", ".join(negatives),
        "camera_prompt": camera_prompt,
        "lighting_prompt": lighting_prompt,
        "sfx_prompt": sfx_prompt,
        "music_prompt": music_prompt,
        "suspense_prompt": suspense,
        "generation_method": "offline_source_mapping",
        "api_calls": 0,
    }


def validate_mapped_scenes(mapped, source_part, part_number):
    source_scenes = source_part.get("scenes")
    if not isinstance(mapped, list) or not isinstance(source_scenes, list):
        raise RuntimeError(f"Invalid mapped scenes for part {part_number}")
    if len(mapped) != len(source_scenes):
        raise RuntimeError(f"Mapped scene count mismatch for part {part_number}")

    validated = []
    for number, item in enumerate(mapped, 1):
        expected_id = f"part_{part_number:02d}_scene_{number:02d}"
        if not isinstance(item, dict):
            raise RuntimeError(f"Invalid mapped scene: {expected_id}")
        if item.get("scene_id") != expected_id:
            raise RuntimeError(f"Scene ID mismatch: {expected_id}")
        if item.get("status") != "completed":
            raise RuntimeError(f"Scene status invalid: {expected_id}")

        for field in SCENE_FIELDS:
            if not isinstance(item.get(field, ""), str):
                raise RuntimeError(f"Invalid {field}: {expected_id}")

        if not clean(item.get("narration")):
            raise RuntimeError(f"Empty narration: {expected_id}")
        if not clean(item.get("visual_prompt")):
            raise RuntimeError(f"Empty visual prompt: {expected_id}")

        validated.append(item)

    return validated


def main():
    log("=" * 60)
    log("KATHA LOK AI — STORY / VISUAL MAPPING")
    log("External API calls: 0")
    log("=" * 60)

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = clean(get_format(config)).lower()
    parts_expected = int(get_parts(config))
    scenes_expected = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full")
    if parts_expected < 1 or scenes_expected < 1:
        raise RuntimeError("PARTS and SCENES must be positive")

    story = read_json(SOURCE_FILE, required=True)
    total, source_part_word_counts = validate_source(
        story, mode, parts_expected, scenes_expected
    )

    plan = validate_plan(
        read_json(PLAN_FILE, required=True),
        mode, parts_expected, scenes_expected
    )

    for p, (source_part, plan_part) in enumerate(
        zip(story["parts"], plan["parts"]), 1
    ):
        for s, (source_scene, plan_scene) in enumerate(
            zip(source_part["scenes"], plan_part["scenes"]), 1
        ):
            expected_id = f"part_{p:02d}_scene_{s:02d}"
            if clean(source_scene.get("scene_id")) not in {"", expected_id}:
                raise RuntimeError(f"Story/plan scene mismatch: {expected_id}")
            if clean(plan_scene.get("scene_id")) != expected_id:
                raise RuntimeError(f"Plan scene mismatch: {expected_id}")

    topic = clean(get_topic(config) or story.get("topic") or story.get("title"))
    source_text = clean(get_story_text(config) or story.get("source_text", ""))
    title = clean(story.get("title") or topic)
    if not title:
        raise RuntimeError("Story title/topic is missing")

    visual_settings = {
        key: config.get(key)
        for key in (
            "VISUAL_STYLE", "REALISM", "CAMERA_STYLE", "LIGHTING", "MOOD",
            "NEGATIVE_PROMPT", "AVOID_NEON", "AVOID_GLITCH_EFFECTS",
            "PART_HOOK", "PART_SUSPENSE", "FINAL_RESOLUTION",
        )
    }

    source_fingerprint = fingerprint({
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "story": story,
        "plan": plan,
        "format": mode,
        "parts": parts_expected,
        "scenes_per_part": scenes_expected,
        "topic": topic,
        "source_text": source_text,
        "visual_settings": visual_settings,
    })

    previous = read_json(OUTPUT_FILE)
    reusable_parts = {}

    if (
        isinstance(previous, dict)
        and previous.get("status") == "completed"
        and previous.get("source_fingerprint") == source_fingerprint
        and previous.get("mapping_version") == MAPPING_VERSION
        and isinstance(previous.get("parts"), list)
    ):
        for saved in previous["parts"]:
            if not isinstance(saved, dict):
                continue
            try:
                number = int(saved.get("part", -1))
            except (TypeError, ValueError):
                continue
            if saved.get("status") == "completed":
                reusable_parts[number] = saved

    result = {
        "status": "in_progress",
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "scene_order": "part_then_scene",
        "title": title,
        "topic": topic,
        "story_text": source_text,
        "format": mode,
        "model": "not_required",
        "generation_method": "offline_source_mapping",
        "api_calls": 0,
        "source_fingerprint": source_fingerprint,
        "total_scenes": total,
        "source_part_word_counts": source_part_word_counts,
        "parts": [],
        "completed_scenes": 0,
        "last_completed": None,
    }
    save_json(result)

    reused_count = 0

    for part_number, source_part in enumerate(story["parts"], 1):
        source_scenes = source_part["scenes"]
        mapped = None
        saved_part = reusable_parts.get(part_number)

        if saved_part:
            try:
                mapped = validate_mapped_scenes(
                    saved_part.get("scenes"), source_part, part_number
                )
                reused_count += len(mapped)
                log(f"Part {part_number}: validated checkpoint reused")
            except (RuntimeError, TypeError, ValueError, KeyError) as exc:
                log(f"Part {part_number}: checkpoint rejected: {exc}")

        if mapped is None:
            mapped = []
            for scene_number, source_scene in enumerate(source_scenes, 1):
                previous_source = (
                    source_scenes[scene_number - 2] if scene_number > 1 else None
                )
                next_source = (
                    source_scenes[scene_number]
                    if scene_number < len(source_scenes) else None
                )
                global_number = (
                    (part_number - 1) * scenes_expected + scene_number
                )
                mapped.append(make_scene(
                    source_scene,
                    previous_source,
                    next_source,
                    part_number,
                    scene_number,
                    global_number,
                    config,
                    title,
                ))

            mapped = validate_mapped_scenes(mapped, source_part, part_number)

        for scene_number, scene in enumerate(mapped, 1):
            scene["global_scene_number"] = (
                (part_number - 1) * scenes_expected + scene_number
            )

        result["parts"].append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "status": "completed",
            "title": clean(source_part.get("title")) or f"भाग {part_number}",
            "hook_required": (
                part_number == 1 or bool_value(config, "PART_HOOK", True)
            ),
            "suspense_required": bool_value(config, "PART_SUSPENSE", True),
            "final_resolution": (
                bool_value(config, "FINAL_RESOLUTION", True)
                and part_number == parts_expected
            ),
            "scenes": mapped,
        })

        result["completed_scenes"] = sum(
            len(part["scenes"]) for part in result["parts"]
        )
        result["last_completed"] = {
            "part": part_number,
            "scene": scenes_expected,
            "scene_id": f"part_{part_number:02d}_scene_{scenes_expected:02d}",
        }
        save_json(result)
        log(
            f"Part {part_number}/{parts_expected} saved; "
            f"{result['completed_scenes']}/{total} scenes mapped"
        )

    all_scenes = [
        scene for part in result["parts"] for scene in part["scenes"]
    ]
    expected_ids = [
        f"part_{part:02d}_scene_{scene:02d}"
        for part in range(1, parts_expected + 1)
        for scene in range(1, scenes_expected + 1)
    ]
    actual_ids = [scene["scene_id"] for scene in all_scenes]

    if len(all_scenes) != total or actual_ids != expected_ids:
        raise RuntimeError("Final scene count or ordering validation failed")

    mapped_part_word_counts = [
        sum(word_count(scene["narration"]) for scene in part["scenes"])
        for part in result["parts"]
    ]

    if mode == "short":
        for p, amount in enumerate(mapped_part_word_counts, 1):
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"Mapped short part {p} has {amount} words; "
                    f"required {SHORT_MIN_WORDS}-{SHORT_MAX_WORDS}"
                )

    result.update({
        "status": "completed",
        "completed_scenes": total,
        "api_calls": 0,
        "part_word_counts": mapped_part_word_counts,
        "validation": {
            "exact_scene_count": True,
            "stable_scene_ids": True,
            "sequential_scene_order": True,
            "all_scenes_have_narration": True,
            "all_scenes_have_visual_prompts": True,
            "story_plan_matches_story": True,
            "short_part_word_counts_valid": (
                all(
                    SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS
                    for amount in mapped_part_word_counts
                ) if mode == "short" else None
            ),
            "source_mapping_only": True,
            "external_api_calls": 0,
        },
    })
    save_json(result)

    log("=" * 60)
    log("STORY / VISUAL MAPPING: SUCCESS")
    log("Format:", mode)
    log("Parts:", parts_expected)
    log("Scenes per part:", scenes_expected)
    log("Total scenes:", total)
    log("Source words per part:", source_part_word_counts)
    log("Mapped words per part:", mapped_part_word_counts)
    log("Reused scenes:", reused_count)
    log("External API calls: 0")
    log("Output:", OUTPUT_FILE)
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log("STORY / VISUAL MAPPING ERROR:", exc, file=sys.stderr)
        sys.exit(1)
