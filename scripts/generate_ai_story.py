
#!/usr/bin/env python3
"""Schema-compatible story mapper with continuity and checkpoint validation."""

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
OUTPUT_FILE = STORY_DIR / "ai_story.json"

VERSION = "offline-ai-story-mapper-v3-story-schema-compatible"
MAPPING_VERSION = 4

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
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise RuntimeError(f"Cannot read {path}: {exc}") from exc
        return None

    if not isinstance(data, dict):
        if required:
            raise RuntimeError(f"Expected a JSON object in {path}.")
        return None
    return data


def save_json(data):
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT_FILE.with_suffix(".json.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, OUTPUT_FILE)
    finally:
        temp.unlink(missing_ok=True)


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


def validate_source(story, mode, parts_expected, scenes_expected):
    if story.get("status") != "completed":
        raise RuntimeError("story.json is not marked completed.")

    if clean(story.get("format")).lower() != mode:
        raise RuntimeError(
            "story.json FORMAT does not match Input/topic.txt. "
            "Regenerate the story first."
        )

    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_expected:
        actual = len(parts) if isinstance(parts, list) else "invalid"
        raise RuntimeError(
            f"story.json has {actual} parts; expected {parts_expected}."
        )

    total = parts_expected * scenes_expected
    global_number = 0

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part {p}.")
        try:
            declared_part = int(part.get("part", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid part number at {p}.") from exc

        if declared_part != p:
            raise RuntimeError(f"Part order mismatch at part {p}.")

        source_scenes = part.get("scenes")
        if (
            not isinstance(source_scenes, list)
            or len(source_scenes) != scenes_expected
        ):
            actual = len(source_scenes) if isinstance(source_scenes, list) else 0
            raise RuntimeError(
                f"Part {p} has {actual} scenes; expected {scenes_expected}."
            )

        for s, scene in enumerate(source_scenes, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid source scene {p}:{s}.")

            try:
                declared_scene = int(scene.get("scene", -1))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid scene number at {p}:{s}.") from exc

            expected_id = f"part_{p:02d}_scene_{s:02d}"
            if declared_scene != s:
                raise RuntimeError(f"Scene number mismatch at {expected_id}.")

            source_id = clean(scene.get("scene_id"))
            if source_id and source_id != expected_id:
                raise RuntimeError(
                    f"Scene ID mismatch at {expected_id}: {source_id}."
                )

            narration = text_value(scene, "narration", "text", "dialogue")
            visual = text_value(
                scene, "visual", "visual_prompt", "image_prompt",
                "scene_prompt", "visual_description", "description",
            )

            if not narration:
                raise RuntimeError(f"Empty narration at {expected_id}.")
            if not visual:
                raise RuntimeError(f"Empty visual description at {expected_id}.")

    if global_number != total:
        raise RuntimeError(
            f"Source scene count mismatch: {global_number}/{total}."
        )

    if not clean(story.get("hook")):
        raise RuntimeError("story.json has no opening hook.")

    return total


def identity_details(scene):
    """Use only explicit source details; do not infer gender or identity."""
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


def source_visual(scene):
    return text_value(
        scene, "visual", "visual_prompt", "image_prompt",
        "scene_prompt", "visual_description", "description",
    )


def make_scene(
    source,
    previous_source,
    next_source,
    part_number,
    scene_number,
    global_number,
    config,
    title,
):
    scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

    narration = text_value(source, "narration", "text", "dialogue")
    visual = source_visual(source)
    dialogue = text_value(source, "dialogue")
    suspense = text_value(source, "suspense", "suspense_prompt")

    realism = clean(config.get("REALISM", "high")) or "high"
    visual_style = (
        clean(config.get("VISUAL_STYLE", "cinematic_realistic"))
        or "cinematic_realistic"
    )
    camera_style = clean(config.get("CAMERA_STYLE", "cinematic")) or "cinematic"
    lighting_style = clean(config.get("LIGHTING", "cinematic")) or "cinematic"
    mood = clean(config.get("MOOD", "dramatic")) or "dramatic"

    location = text_value(source, "location", "setting", "world_context")
    time_period = text_value(source, "time_period", "time", "era")
    identity = identity_details(source)

    context = []
    if title:
        context.append(f"story title/context: {title}")
    if location:
        context.append(f"location: {location}")
    if time_period:
        context.append(f"time period: {time_period}")
    if identity:
        context.append("explicit character details: " + identity)

    if previous_source:
        previous_identity = identity_details(previous_source)
        previous_visual = source_visual(previous_source)
        if previous_identity:
            context.append(
                "previous-scene identity continuity: preserve compatible "
                "explicit details only (" + previous_identity + ")"
            )
        if previous_visual:
            context.append("previous scene: " + previous_visual[:220])

    if next_source:
        next_visual = source_visual(next_source)
        if next_visual:
            context.append("next-scene continuity reference: " + next_visual[:180])

    context_text = " ".join(context)

    visual_prompt = (
        f"Story scene: {visual}. "
        f"Narrative context: {narration[:500]}. "
        f"{context_text} "
        f"Visual style: {visual_style}; realism: {realism}. "
        "Photorealistic live-action cinematic photography, believable anatomy, "
        "natural skin texture, realistic materials, physically plausible "
        "lighting and environmental detail. Keep character faces, age, "
        "clothing and identity consistent when explicit source information "
        "exists. Do not infer or change gender, age, relationship or identity. "
        "Show one coherent moment with motivated composition, not a collage "
        "or slideshow. Do not render text, captions, logos or watermarks."
    )

    camera_prompt = (
        f"{camera_style} camera language chosen for the described action. "
        "Use an establishing wide shot for locations, medium framing for "
        "interaction and close-ups for important expressions or clues. "
        "Keep camera movement physically plausible and purposeful. "
        "Avoid identical framing in every scene, arbitrary dutch angles, "
        "unmotivated movement and framing that contradicts the action."
    )

    lighting_prompt = (
        f"{lighting_style} lighting matched to the scene's location, time "
        f"and mood ({mood}). Preserve natural skin tones, realistic shadow "
        "direction, highlight detail and shadow detail. Avoid crushed blacks, "
        "blown highlights, excessive contrast, oversaturation and arbitrary "
        "neon lighting unless the scene specifically requires it."
    )

    negative_parts = [
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
        "unsupported gender or age change",
        "unmotivated extreme contrast",
        "blown highlights",
        "crushed shadows",
    ]

    configured_negative = clean(config.get("NEGATIVE_PROMPT"))
    if configured_negative:
        negative_parts.append(configured_negative)

    if bool_value(config, "AVOID_NEON", True):
        negative_parts.append("unmotivated neon lighting")
    if bool_value(config, "AVOID_GLITCH_EFFECTS", True):
        negative_parts.append("glitch effects")

    sfx_prompt = (
        f"Scene-relevant sound effects for: {visual[:300]}. "
        "Align effects with visible actions and narration. Use silence when "
        "appropriate. Avoid unrelated impacts, unrequested voices and "
        "repetitive dramatic stingers."
    )

    music_prompt = (
        f"{mood} cinematic background music appropriate to this scene. "
        "Maintain continuity between connected scenes. Keep music beneath "
        "the narration, avoid abrupt volume changes, lyrics and intelligible "
        "speech. Do not substitute music for scene-specific sound effects."
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
        "negative_prompt": ", ".join(negative_parts),
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
        raise RuntimeError(f"Invalid mapped scenes for part {part_number}.")
    if len(mapped) != len(source_scenes):
        raise RuntimeError(f"Mapped scene count mismatch for part {part_number}.")

    validated = []
    for scene_number, item in enumerate(mapped, 1):
        expected_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
        if not isinstance(item, dict):
            raise RuntimeError(f"Invalid mapped record: {expected_id}.")
        if item.get("scene_id") != expected_id:
            raise RuntimeError(f"Scene ID mismatch: {expected_id}.")
        if item.get("status") != "completed":
            raise RuntimeError(f"Scene is not completed: {expected_id}.")

        for field in SCENE_FIELDS:
            if not isinstance(item.get(field, ""), str):
                raise RuntimeError(f"Invalid {field} in {expected_id}.")

        if not clean(item.get("narration")):
            raise RuntimeError(f"Empty narration in {expected_id}.")
        if not clean(item.get("visual_prompt")):
            raise RuntimeError(f"Empty visual prompt in {expected_id}.")

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
        raise RuntimeError("FORMAT must be short or full.")
    if parts_expected < 1 or scenes_expected < 1:
        raise RuntimeError("PARTS and SCENES must be positive.")

    story = read_json(SOURCE_FILE, required=True)
    total = validate_source(story, mode, parts_expected, scenes_expected)

    topic = clean(get_topic(config) or story.get("topic") or story.get("title"))
    source_text = clean(
        get_story_text(config) or story.get("source_text", "")
    )
    story_title = clean(story.get("title") or topic)

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
        )
    }

    source_fingerprint = fingerprint({
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "story": story,
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
        for saved_part in previous["parts"]:
            if not isinstance(saved_part, dict):
                continue
            try:
                number = int(saved_part.get("part", -1))
            except (TypeError, ValueError):
                continue
            if saved_part.get("status") == "completed":
                reusable_parts[number] = saved_part

    result = {
        "status": "in_progress",
        "version": VERSION,
        "mapping_version": MAPPING_VERSION,
        "scene_order": "part_then_scene",
        "topic": topic,
        "story_text": source_text,
        "format": mode,
        "model": "not_required",
        "generation_method": "offline_source_mapping",
        "api_calls": 0,
        "source_fingerprint": source_fingerprint,
        "total_scenes": total,
        "parts": [],
        "completed_scenes": 0,
        "last_completed": None,
    }
    save_json(result)

    reused_count = 0

    for part_number, source_part in enumerate(story["parts"], 1):
        source_scenes = source_part["scenes"]
        saved_part = reusable_parts.get(part_number)
        mapped = None

        if saved_part:
            try:
                mapped = validate_mapped_scenes(
                    saved_part.get("scenes"), source_part, part_number
                )
                reused_count += len(mapped)
                log(f"Part {part_number}: validated checkpoint reused.")
            except (RuntimeError, TypeError, ValueError, KeyError) as exc:
                log(f"Part {part_number}: checkpoint rejected: {exc}")

        if mapped is None:
            mapped = []
            for scene_number, source_scene in enumerate(source_scenes, 1):
                previous_source = (
                    source_scenes[scene_number - 2]
                    if scene_number > 1 else None
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
                    story_title,
                ))

            mapped = validate_mapped_scenes(
                mapped, source_part, part_number
            )

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
            "scenes": mapped,
        })

        result["completed_scenes"] = sum(
            len(part["scenes"]) for part in result["parts"]
        )
        result["last_completed"] = {
            "part": part_number,
            "scene": scenes_expected,
            "scene_id": (
                f"part_{part_number:02d}_scene_{scenes_expected:02d}"
            ),
        }
        save_json(result)
        log(
            f"Part {part_number}/{parts_expected} saved; "
            f"{result['completed_scenes']}/{total} scenes mapped."
        )

    all_scenes = [
        scene
        for part in result["parts"]
        for scene in part["scenes"]
    ]
    expected_ids = [
        f"part_{part:02d}_scene_{scene:02d}"
        for part in range(1, parts_expected + 1)
        for scene in range(1, scenes_expected + 1)
    ]
    actual_ids = [scene["scene_id"] for scene in all_scenes]

    if len(all_scenes) != total or actual_ids != expected_ids:
        raise RuntimeError("Final scene count or ordering validation failed.")

    result.update({
        "status": "completed",
        "completed_scenes": total,
        "api_calls": 0,
        "validation": {
            "exact_scene_count": True,
            "stable_scene_ids": True,
            "sequential_scene_order": True,
            "all_scenes_have_narration": True,
            "all_scenes_have_visual_prompts": True,
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
