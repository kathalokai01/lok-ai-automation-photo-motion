#!/usr/bin/env python3
"""Katha Lok AI — schema-compatible offline story-to-scene mapper."""

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
PLAN_CANDIDATES = (
    ROOT / "output" / "config" / "story_plan.json",
    STORY_DIR / "story_plan.json",
)
OUTPUT_FILE = STORY_DIR / "ai_story.json"
CHECKPOINT_FILE = STORY_DIR / "ai_story.checkpoint.json"

VERSION = "offline-ai-story-mapper-v6-schema-compatible"
MAPPING_VERSION = 6
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
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
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
            raise RuntimeError(f"Expected a JSON object: {path}")
        return None
    return value


def write_json(path, data):
    path = Path(path)
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
        scene,
        "visual",
        "visual_prompt",
        "image_prompt",
        "scene_prompt",
        "visual_description",
        "description",
    )


def expected_scene_id(part, scene):
    return f"part_{part:02d}_scene_{scene:02d}"


def source_scene_id(part, scene):
    return f"p{part:02d}_s{scene:02d}"


def load_plan():
    for path in PLAN_CANDIDATES:
        data = read_json(path)
        if data is not None:
            log("Using story plan:", path.relative_to(ROOT).as_posix())
            return data, path
    raise RuntimeError(
        "story_plan.json not found in output/config/ or output/story/"
    )


def validate_plan(plan, mode, parts_expected, scenes_expected):
    if clean(plan.get("status")).lower() != "planned":
        raise RuntimeError("story_plan.json status must be planned")

    if clean(plan.get("format")).lower() != mode:
        raise RuntimeError("Story plan FORMAT does not match input")

    expected_total = parts_expected * scenes_expected

    checks = {
        "parts_count": parts_expected,
        "scenes_per_part": scenes_expected,
        "total_scenes": expected_total,
    }
    for key, expected in checks.items():
        try:
            actual = int(plan.get(key, -1))
        except (TypeError, ValueError):
            actual = -1
        if actual != expected:
            raise RuntimeError(f"Story plan {key} mismatch")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_expected:
        raise RuntimeError("Story plan parts list mismatch")

    global_number = 0
    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != p:
            raise RuntimeError(f"Story plan part numbering mismatch: {p}")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_expected:
            raise RuntimeError(f"Story plan scene count mismatch: part {p}")

        for s, scene in enumerate(scenes, 1):
            global_number += 1
            expected_id = expected_scene_id(p, s)

            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid planned scene: {expected_id}")

            if clean(scene.get("scene_id")) != expected_id:
                raise RuntimeError(f"Planned scene ID mismatch: {expected_id}")

            if int(scene.get("scene", -1)) != s:
                raise RuntimeError(f"Planned scene number mismatch: {expected_id}")

            if int(scene.get("global_scene_number", -1)) != global_number:
                raise RuntimeError(f"Planned global scene mismatch: {expected_id}")

            if not text_value(scene, "purpose", "beat_description"):
                raise RuntimeError(f"Planned scene purpose missing: {expected_id}")

    return plan


def validate_source(story, mode, parts_expected, scenes_expected):
    if story.get("status") != "completed":
        raise RuntimeError("story.json is not marked completed")

    if clean(story.get("format")).lower() != mode:
        raise RuntimeError("story.json FORMAT mismatch; regenerate story first")

    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_expected:
        actual = len(parts) if isinstance(parts, list) else "invalid"
        raise RuntimeError(
            f"story.json has {actual} parts; expected {parts_expected}"
        )

    expected_total = parts_expected * scenes_expected
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
            expected_id = expected_scene_id(p, s)

            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid source scene: {expected_id}")

            if int(scene.get("scene", -1)) != s:
                raise RuntimeError(f"Source scene number mismatch: {expected_id}")

            actual_id = clean(scene.get("scene_id"))
            accepted_ids = {
                "",
                expected_id,
                source_scene_id(p, s),
            }
            if actual_id not in accepted_ids:
                raise RuntimeError(
                    f"Source scene ID mismatch at {expected_id}: {actual_id}"
                )

            narration = text_value(scene, "narration", "text", "dialogue")
            visual = source_visual(scene)

            if not narration:
                raise RuntimeError(f"Empty narration: {expected_id}")
            if not visual:
                raise RuntimeError(f"Empty visual description: {expected_id}")

            count += word_count(narration)

        part_counts.append(count)

    if global_number != expected_total:
        raise RuntimeError("Source total scene count mismatch")

    if mode == "short":
        for p, amount in enumerate(part_counts, 1):
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"SHORT STORY INPUT ERROR: part {p} has {amount} words; "
                    f"required {SHORT_MIN_WORDS}-{SHORT_MAX_WORDS}. "
                    "Correct the story-generation stage; this offline mapper "
                    "does not invent or rewrite narration."
                )

    return expected_total, part_counts


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
        names = [clean(item) for item in characters if clean(item)]
        if names:
            details.append("named characters: " + ", ".join(names))
    elif isinstance(characters, str) and clean(characters):
        details.append("named characters: " + clean(characters))

    return "; ".join(details)


def make_scene(
    source,
    previous_source,
    next_source,
    part_number,
    scene_number,
    global_number,
    config,
    title,
    plan_scene,
):
    scene_id = expected_scene_id(part_number, scene_number)
    narration = text_value(source, "narration", "text", "dialogue")
    visual = source_visual(source)
    dialogue = text_value(source, "dialogue")
    suspense = text_value(
        source,
        "suspense",
        "suspense_prompt",
        default=text_value(plan_scene, "suspense", "suspense_prompt"),
    )

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

    plan_purpose = text_value(plan_scene, "purpose", "beat_description")
    plan_anchor = text_value(plan_scene, "story_anchor", "beat")

    visual_prompt = (
        f"Story scene: {visual}. Narrative context: {narration[:500]}. "
        f"{' '.join(context)} Story beat: {plan_purpose}. "
        f"Story anchor: {plan_anchor}. "
        f"Visual style: {style}; realism: {realism}. "
        "Photorealistic live-action cinematic photography, believable anatomy, "
        "natural skin texture, realistic materials and physically plausible "
        "lighting. Preserve explicit character face, age, clothing and identity "
        "between connected scenes. Show one coherent moment, not a collage "
        "or slideshow. No text, captions, logos or watermarks."
    )

    camera_prompt = (
        f"{camera} camera language suited to the action. Use establishing "
        "wide shots for locations, medium framing for interactions and "
        "close-ups for important expressions or clues. Vary framing with "
        "purpose and keep camera movement physically plausible."
    )

    lighting_prompt = (
        f"{lighting} lighting appropriate to the scene location, time and "
        f"mood ({mood}). Preserve natural skin tones, shadow detail and "
        "highlight detail. Avoid crushed blacks, blown highlights and "
        "oversaturation unless required by the scene."
    )

    negatives = [
        "cartoon",
        "comic illustration",
        "anime",
        "slideshow",
        "collage",
        "unintended text",
        "embedded subtitles",
        "watermark",
        "logo",
        "distorted anatomy",
        "duplicate people",
        "inconsistent character identity",
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
        "Align effects with visible actions. Use silence when appropriate; "
        "avoid unrelated impacts and repetitive stingers."
    )

    music_prompt = (
        f"{mood} cinematic background music appropriate to this scene. "
        "Maintain continuity between connected scenes. Keep music beneath "
        "narration; avoid abrupt volume changes, lyrics and intelligible speech."
    )

    return {
        "part": part_number,
        "scene": scene_number,
        "local_scene": scene_number,
        "part_id": f"part_{part_number:02d}",
        "scene_id": scene_id,
        "source_scene_id": clean(source.get("scene_id")) or source_scene_id(
            part_number, scene_number
        ),
        "global_scene_number": global_number,
        "mapping_version": MAPPING_VERSION,
        "role": text_value(source, "purpose", "role", "scene_purpose")
        or plan_purpose,
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

def validate_mapped_scene(item, expected, part_number, scene_number):
    if not isinstance(item, dict):
        raise RuntimeError("Invalid mapped scene record")

    for key in ("part", "scene", "global_scene_number"):
        if int(item.get(key, -1)) != int(expected[key]):
            raise RuntimeError(
                f"Mapped scene {key} mismatch: "
                f"part {part_number}, scene {scene_number}"
            )

    if item.get("scene_id") != expected["scene_id"]:
        raise RuntimeError(
            f"Mapped scene ID mismatch: {expected['scene_id']}"
        )

    if item.get("status") != "completed":
        raise RuntimeError(f"Mapped scene incomplete: {expected['scene_id']}")

    for field in SCENE_FIELDS:
        if not isinstance(item.get(field, ""), str):
            raise RuntimeError(
                f"Invalid {field}: {expected['scene_id']}"
            )

    if not clean(item.get("narration")):
        raise RuntimeError(f"Empty narration: {expected['scene_id']}")

    if not clean(item.get("visual_prompt")):
        raise RuntimeError(f"Empty visual prompt: {expected['scene_id']}")

    if item["narration"] != expected["narration"]:
        raise RuntimeError(
            f"Checkpoint narration differs from source: {expected['scene_id']}"
        )

    return item


def make_result(
    status,
    source_fingerprint,
    mode,
    title,
    topic,
    story_text,
    parts_count,
    scenes_per_part,
    parts,
    total_scenes,
    source_part_word_counts,
    reused_count,
):
    all_scenes = [
        scene
        for part in parts
        for scene in part.get("scenes", [])
    ]

    return {
        "status": status,
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "scene_order": "part_then_scene",
        "title": title,
        "topic": topic,
        "story_text": story_text,
        "format": mode,
        "model": "not_required",
        "generation_method": "offline_source_mapping",
        "api_calls": 0,
        "source_fingerprint": source_fingerprint,
        "total_parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total_scenes,
        "completed_scenes": len(all_scenes),
        "reused_scenes": reused_count,
        "source_part_word_counts": source_part_word_counts,
        "part_word_counts": [
            sum(word_count(scene.get("narration", "")) for scene in part["scenes"])
            for part in parts
        ],
        "parts": parts,
        "scenes": all_scenes,
        "validation": {
            "exact_scene_count": len(all_scenes) == total_scenes,
            "stable_scene_ids": True,
            "sequential_scene_order": True,
            "narration_matches_source": True,
            "visual_prompts_present": True,
            "story_plan_matches_input": True,
            "short_part_word_counts_valid": (
                all(
                    SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS
                    for amount in source_part_word_counts
                )
                if mode == "short"
                else None
            ),
            "external_api_calls": 0,
        },
    }


def main():
    log("=" * 60)
    log("KATHA LOK AI — SCHEMA-COMPATIBLE STORY MAPPING")
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
    total_scenes, source_part_word_counts = validate_source(
        story, mode, parts_expected, scenes_expected
    )

    plan, plan_path = load_plan()
    validate_plan(plan, mode, parts_expected, scenes_expected)

    for p, (source_part, plan_part) in enumerate(
        zip(story["parts"], plan["parts"]), 1
    ):
        for s, (source_scene, plan_scene) in enumerate(
            zip(source_part["scenes"], plan_part["scenes"]), 1
        ):
            source_id = clean(source_scene.get("scene_id"))
            accepted = {
                "",
                expected_scene_id(p, s),
                source_scene_id(p, s),
            }
            if source_id not in accepted:
                raise RuntimeError(
                    f"Story scene ID does not match plan at {p}:{s}"
                )

            if clean(plan_scene.get("scene_id")) != expected_scene_id(p, s):
                raise RuntimeError(f"Plan scene ID mismatch at {p}:{s}")

    topic = clean(
        get_topic(config)
        or story.get("topic")
        or story.get("title")
    )
    title = clean(
        story.get("title")
        or story.get("final_title")
        or story.get("generated_title")
        or topic
    )

    if not title:
        raise RuntimeError(
            "Story title missing. Generate the title in generate_story.py first."
        )

    story_text = clean(
        story.get("story_text")
        or story.get("source_text")
        or get_story_text(config)
    )

    visual_settings = {
        key: config.get(key)
        for key in (
            "VISUAL_STYLE",
            "REALISM",
            "CAMERA_STYLE",
            "LIGHTING",
            "MOOD",
            "NEGATIVE_PROMPT",
            "AVOID_NEON",
            "AVOID_GLITCH_EFFECTS",
            "PART_HOOK",
            "PART_SUSPENSE",
            "FINAL_RESOLUTION",
        )
    }

    source_fingerprint = fingerprint({
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "story": story,
        "plan": plan,
        "plan_path": plan_path.relative_to(ROOT).as_posix(),
        "format": mode,
        "parts": parts_expected,
        "scenes_per_part": scenes_expected,
        "topic": topic,
        "visual_settings": visual_settings,
    })

    old = read_json(OUTPUT_FILE)
    old_scenes = {}

    if (
        isinstance(old, dict)
        and old.get("status") in {"completed", "in_progress"}
        and old.get("source_fingerprint") == source_fingerprint
        and old.get("mapping_version") == MAPPING_VERSION
        and isinstance(old.get("scenes"), list)
    ):
        for item in old["scenes"]:
            if not isinstance(item, dict):
                continue
            try:
                key = (int(item["part"]), int(item["scene"]))
            except (KeyError, TypeError, ValueError):
                continue
            if item.get("status") == "completed":
                old_scenes[key] = item

    checkpoint = read_json(CHECKPOINT_FILE)
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("source_fingerprint") != source_fingerprint
        or checkpoint.get("mapping_version") != MAPPING_VERSION
    ):
        checkpoint = {
            "status": "in_progress",
            "version": VERSION,
            "mapping_version": MAPPING_VERSION,
            "source_fingerprint": source_fingerprint,
            "completed_scenes": [],
        }

    checkpoint_scenes = {}
    for item in checkpoint.get("completed_scenes", []):
        if not isinstance(item, dict):
            continue
        try:
            key = (int(item["part"]), int(item["scene"]))
        except (KeyError, TypeError, ValueError):
            continue
        checkpoint_scenes[key] = item

    result_parts = []
    reused_count = 0
    global_number = 0

    for part_number, source_part in enumerate(story["parts"], 1):
        mapped_scenes = []
        source_scenes = source_part["scenes"]
        plan_scenes = plan["parts"][part_number - 1]["scenes"]

        for scene_number, source_scene in enumerate(source_scenes, 1):
            global_number += 1
            key = (part_number, scene_number)
            plan_scene = plan_scenes[scene_number - 1]

            previous_source = (
                source_scenes[scene_number - 2]
                if scene_number > 1
                else None
            )
            next_source = (
                source_scenes[scene_number]
                if scene_number < len(source_scenes)
                else None
            )

            expected = make_scene(
                source_scene,
                previous_source,
                next_source,
                part_number,
                scene_number,
                global_number,
                config,
                title,
                plan_scene,
            )

            saved = checkpoint_scenes.get(key) or old_scenes.get(key)
            mapped = None

            if saved:
                try:
                    mapped = validate_mapped_scene(
                        saved, expected, part_number, scene_number
                    )
                    reused_count += 1
                except (RuntimeError, TypeError, ValueError, KeyError):
                    mapped = None

            if mapped is None:
                mapped = expected

            mapped_scenes.append(mapped)

            checkpoint["completed_scenes"] = [
                scene
                for scene in checkpoint.get("completed_scenes", [])
                if not (
                    int(scene.get("part", -1)) == part_number
                    and int(scene.get("scene", -1)) == scene_number
                )
            ]
            checkpoint["completed_scenes"].append(mapped)
            checkpoint["status"] = "in_progress"
            write_json(CHECKPOINT_FILE, checkpoint)

            partial_parts = list(result_parts)
            partial_parts.append({
                "part": part_number,
                "part_id": f"part_{part_number:02d}",
                "status": "in_progress",
                "title": clean(source_part.get("title")) or f"भाग {part_number}",
                "scenes": list(mapped_scenes),
            })

            partial_result = make_result(
                "in_progress",
                source_fingerprint,
                mode,
                title,
                topic,
                story_text,
                parts_expected,
                scenes_expected,
                partial_parts,
                total_scenes,
                source_part_word_counts,
                reused_count,
            )
            write_json(OUTPUT_FILE, partial_result)

        result_parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "status": "completed",
            "title": clean(source_part.get("title")) or f"भाग {part_number}",
            "hook_required": (
                part_number == 1
                or bool_value(config, "PART_HOOK", True)
            ),
            "suspense_required": bool_value(
                config, "PART_SUSPENSE", True
            ),
            "final_resolution": (
                bool_value(config, "FINAL_RESOLUTION", True)
                and part_number == parts_expected
            ),
            "scenes": mapped_scenes,
        })

        log(
            f"Part {part_number}/{parts_expected} saved; "
            f"{sum(len(item['scenes']) for item in result_parts)}/"
            f"{total_scenes} scenes mapped"
        )

    all_scenes = [
        scene
        for part in result_parts
        for scene in part["scenes"]
    ]

    expected_ids = [
        expected_scene_id(p, s)
        for p in range(1, parts_expected + 1)
        for s in range(1, scenes_expected + 1)
    ]
    actual_ids = [scene.get("scene_id") for scene in all_scenes]

    if len(all_scenes) != total_scenes or actual_ids != expected_ids:
        raise RuntimeError("Final scene count or ordering validation failed")

    final_result = make_result(
        "completed",
        source_fingerprint,
        mode,
        title,
        topic,
        story_text,
        parts_expected,
        scenes_expected,
        result_parts,
        total_scenes,
        source_part_word_counts,
        reused_count,
    )

    final_result["validation"].update({
        "exact_scene_count": True,
        "stable_scene_ids": True,
        "sequential_scene_order": True,
        "narration_matches_source": True,
        "visual_prompts_present": True,
        "story_plan_matches_input": True,
    })

    write_json(OUTPUT_FILE, final_result)

    checkpoint["status"] = "completed"
    checkpoint["total_scenes"] = total_scenes
    checkpoint["completed_count"] = total_scenes
    write_json(CHECKPOINT_FILE, checkpoint)

    log("=" * 60)
    log("STORY / VISUAL MAPPING: SUCCESS")
    log("Format:", mode)
    log("Parts:", parts_expected)
    log("Scenes per part:", scenes_expected)
    log("Total scenes:", total_scenes)
    log("Source words per part:", source_part_word_counts)
    log("Reused scenes:", reused_count)
    log("External API calls: 0")
    log("Output:", OUTPUT_FILE)
    log("Checkpoint:", CHECKPOINT_FILE)
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