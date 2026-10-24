
#!/usr/bin/env python3
"""Schema-compatible offline story-to-visual prompt mapper."""

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

VERSION = "offline-ai-story-mapper-v2-continuity-prompts"

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


def validate_source(story, parts_expected, scenes_expected):
    if story.get("status") != "completed":
        raise RuntimeError("story.json is not marked completed.")

    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_expected:
        actual = len(parts) if isinstance(parts, list) else "invalid"
        raise RuntimeError(
            f"story.json has {actual} parts; expected {parts_expected}."
        )

    total = parts_expected * scenes_expected
    global_number = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part {part_number}.")
        try:
            actual_part = int(part.get("part", -1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid part number at part {part_number}."
            ) from exc

        if actual_part != part_number:
            raise RuntimeError(f"Part numbering mismatch at {part_number}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_expected:
            actual = len(scenes) if isinstance(scenes, list) else "invalid"
            raise RuntimeError(
                f"Part {part_number} has {actual} scenes; "
                f"expected {scenes_expected}."
            )

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Invalid source scene {part_number}:{scene_number}."
                )

            try:
                actual_scene = int(scene.get("scene", -1))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid scene number at {part_number}:{scene_number}."
                ) from exc

            if actual_scene != scene_number:
                raise RuntimeError(
                    f"Scene numbering mismatch at "
                    f"{part_number}:{scene_number}."
                )

            narration = clean(scene.get("narration"))
            visual = clean(
                scene.get("visual")
                or scene.get("visual_prompt")
                or scene.get("image_prompt")
            )
            if not narration:
                raise RuntimeError(
                    f"Empty narration at {part_number}:{scene_number}."
                )
            if not visual:
                raise RuntimeError(
                    f"Empty visual description at "
                    f"{part_number}:{scene_number}."
                )

    if global_number != total:
        raise RuntimeError(
            f"Source scene count mismatch: {global_number}/{total}."
        )

    if not clean(story.get("hook")):
        raise RuntimeError("story.json has no opening hook.")

    return total


def scene_value(scene, *keys):
    for key in keys:
        value = clean(scene.get(key))
        if value:
            return value
    return ""


def scene_identity_details(scene):
    """Use explicit source details only; never guess a character's gender."""
    fields = (
        ("character", ("character_name", "character", "person_name")),
        ("gender", ("gender",)),
        ("age", ("age",)),
        ("appearance", ("appearance",)),
        ("clothing", ("clothing",)),
        ("hair", ("hair",)),
        ("distinctive features", ("distinctive_features",)),
    )

    details = []
    for label, keys in fields:
        value = scene_value(scene, *keys)
        if value:
            details.append(f"{label}: {value}")

    names = scene.get("characters")
    if isinstance(names, list):
        names = [clean(item) for item in names if clean(item)]
        if names:
            details.append("named characters: " + ", ".join(names))
    elif isinstance(names, str) and clean(names):
        details.append("named characters: " + clean(names))

    return "; ".join(details)


def make_scene(
    source,
    previous_source,
    next_source,
    part_number,
    scene_number,
    global_number,
    config,
    story_title,
):
    scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

    narration = clean(source.get("narration"))
    visual = clean(
        source.get("visual")
        or source.get("visual_prompt")
        or source.get("image_prompt")
    )
    dialogue = clean(source.get("dialogue"))
    suspense = clean(source.get("suspense"))

    realism = clean(config.get("REALISM", "high")) or "high"
    visual_style = (
        clean(config.get("VISUAL_STYLE", "cinematic_realistic"))
        or "cinematic_realistic"
    )
    camera_style = clean(config.get("CAMERA_STYLE", "cinematic")) or "cinematic"
    lighting_style = (
        clean(config.get("LIGHTING", "cinematic")) or "cinematic"
    )
    mood = clean(config.get("MOOD", "dramatic")) or "dramatic"

    location = scene_value(source, "location", "setting")
    time_period = scene_value(source, "time_period", "time", "era")
    explicit_identity = scene_identity_details(source)

    continuity = []
    if story_title:
        continuity.append(f"story context: {story_title}")
    if location:
        continuity.append(f"location: {location}")
    if time_period:
        continuity.append(f"time period: {time_period}")
    if explicit_identity:
        continuity.append(f"explicit character details: {explicit_identity}")

    if previous_source:
        previous_visual = scene_value(
            previous_source, "visual", "visual_prompt", "image_prompt"
        )
        previous_identity = scene_identity_details(previous_source)
        if previous_identity:
            continuity.append(
                "continuity with preceding scene: preserve only compatible "
                "explicit character details (" + previous_identity + ")"
            )
        if previous_visual:
            continuity.append(
                "preceding scene context: " + previous_visual[:220]
            )

    if next_source:
        next_visual = scene_value(
            next_source, "visual", "visual_prompt", "image_prompt"
        )
        if next_visual:
            continuity.append(
                "following scene context for continuity: " + next_visual[:180]
            )

    continuity_text = " ".join(continuity)
    visual_prompt = (
        f"Story scene: {visual}. "
        f"Narrative context: {narration[:500]}. "
        f"{continuity_text} "
        f"Visual treatment: {visual_style}; realism: {realism}; "
        "photorealistic live-action cinematic photography. "
        "Use believable anatomy, natural skin texture, physically plausible "
        "materials, motivated light sources, natural exposure and realistic "
        "environmental detail. Keep characters visually consistent with "
        "explicit source descriptions and nearby scenes. Do not invent a "
        "gender, age, relationship, costume, or identity not supported by "
        "the source. Show the described moment, not a collage or slideshow. "
        "No text, captions, logos or watermarks."
    )

    camera_prompt = (
        f"{camera_style} camera language suited to this exact action. "
        "Choose framing based on the scene: establishing wide shot for place, "
        "medium shot for interaction, close-up for a meaningful expression "
        "or clue. Use physically plausible movement and stable composition. "
        "Avoid arbitrary camera motion, repeated identical framing, "
        "unmotivated dutch angles and movement that contradicts the action."
    )

    lighting_prompt = (
        f"{lighting_style} lighting matched to the described location, "
        "time and mood ({mood}). Use believable key light, natural fill, "
        "consistent shadow direction, realistic skin tones and retained "
        "detail in highlights and shadows. Avoid crushed blacks, blown-out "
        "highlights, excessive contrast, oversaturated colors and arbitrary "
        "neon unless explicitly required by the scene."
    )

    negative_parts = [
        "cartoon",
        "comic illustration",
        "slideshow",
        "unintended text",
        "subtitles embedded in image",
        "watermarks",
        "logos",
        "distorted anatomy",
        "duplicate people",
        "inconsistent character identity",
        "unrequested gender or age changes",
        "unmotivated extreme contrast",
        "blown highlights",
        "crushed shadows",
    ]

    configured_negative = clean(config.get("NEGATIVE_PROMPT"))
    if configured_negative:
        negative_parts.append(configured_negative)

    if str(config.get("AVOID_NEON", True)).lower() not in {
        "false", "0", "no"
    }:
        negative_parts.append("unmotivated neon lighting")

    if str(config.get("AVOID_GLITCH_EFFECTS", True)).lower() not in {
        "false", "0", "no"
    }:
        negative_parts.append("glitch effects")

    sfx_prompt = (
        f"Use restrained, scene-relevant sound effects for this visual: "
        f"{visual[:300]}. Align effects with visible actions and the actual "
        "narration timing. Use silence when appropriate. No unrelated impacts, "
        "unrequested voices, or repetitive dramatic stingers."
    )

    music_prompt = (
        f"{mood} cinematic background music appropriate to this scene. "
        "Maintain continuity between connected scenes; keep music beneath "
        "the narration, avoid abrupt volume jumps, and do not add lyrics or "
        "intelligible speech. Music must not replace scene-specific sound."
    )

    return {
        "part": part_number,
        "scene": scene_number,
        "part_id": f"part_{part_number:02d}",
        "scene_id": scene_id,
        "global_scene_number": global_number,
        "mapping_version": 4,
        "source_scene_id": clean(source.get("scene_id")) or scene_id,
        "role": clean(source.get("purpose") or source.get("role")),
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


def validate_mapped_scenes(scenes, source_part, part_number):
    source_scenes = source_part["scenes"]
    if not isinstance(scenes, list) or len(scenes) != len(source_scenes):
        raise RuntimeError(
            f"Invalid mapped scene count for part {part_number}."
        )

    validated = []
    for scene_number, item in enumerate(scenes, 1):
        expected_id = f"part_{part_number:02d}_scene_{scene_number:02d}"

        if not isinstance(item, dict):
            raise RuntimeError(f"Invalid mapped record: {expected_id}.")
        if item.get("scene_id") != expected_id:
            raise RuntimeError(f"Scene ID mismatch: {expected_id}.")
        if item.get("status") != "completed":
            raise RuntimeError(f"Scene is not completed: {expected_id}.")

        for field in SCENE_FIELDS:
            if not isinstance(item.get(field, ""), str):
                raise RuntimeError(
                    f"Invalid {field} field in {expected_id}."
                )

        if not clean(item.get("narration")):
            raise RuntimeError(f"Empty narration in {expected_id}.")
        if not clean(item.get("visual_prompt")):
            raise RuntimeError(f"Empty visual prompt in {expected_id}.")

        validated.append(item)

    return validated


def main():
    log("=" * 60)
    log("KATHA LOK AI — OFFLINE STORY VISUAL MAPPING")
    log("External API calls: 0")
    log("=" * 60)

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    format_type = clean(get_format(config)).lower()
    parts_expected = int(get_parts(config))
    scenes_expected = int(get_scenes(config))

    if format_type not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")
    if parts_expected < 1 or scenes_expected < 1:
        raise RuntimeError("PARTS and SCENES must be positive.")

    story = read_json(SOURCE_FILE, required=True)
    total = validate_source(story, parts_expected, scenes_expected)

    topic = clean(get_topic(config) or story.get("topic") or story.get("title"))
    story_text = clean(
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
            "AVOID_CARTOON_LOOK",
            "AVOID_NEON",
            "AVOID_GLITCH_EFFECTS",
        )
    }

    source_fingerprint = fingerprint({
        "version": VERSION,
        "story": story,
        "format": format_type,
        "parts": parts_expected,
        "scenes_per_part": scenes_expected,
        "topic": topic,
        "story_text": story_text,
        "visual_settings": visual_settings,
    })

    previous = read_json(OUTPUT_FILE)
    reusable_parts = {}

    if (
        isinstance(previous, dict)
        and previous.get("source_fingerprint") == source_fingerprint
        and previous.get("mapping_version") == 4
        and isinstance(previous.get("parts"), list)
    ):
        for part in previous["parts"]:
            if not isinstance(part, dict):
                continue
            try:
                number = int(part.get("part", -1))
            except (TypeError, ValueError):
                continue
            if part.get("status") == "completed":
                reusable_parts[number] = part

    result = {
        "status": "in_progress",
        "version": VERSION,
        "mapping_version": 4,
        "scene_order": "part_then_scene",
        "topic": topic,
        "story_text": story_text,
        "format": format_type,
        "model": "not_required",
        "generation_method": "offline_source_mapping",
        "api_calls": 0,
        "source_fingerprint": source_fingerprint,
        "total_scenes": total,
        "parts": [],
    }

    save_json(result)
    reused_count = 0

    for part_number, source_part in enumerate(story["parts"], 1):
        source_scenes = source_part["scenes"]
        saved = reusable_parts.get(part_number)
        mapped = None

        if saved:
            try:
                mapped = validate_mapped_scenes(
                    saved.get("scenes"), source_part, part_number
                )
                reused_count += len(mapped)
                log(f"Part {part_number}: reused validated checkpoint.")
            except (RuntimeError, TypeError, ValueError, KeyError) as exc:
                log(
                    f"Part {part_number}: checkpoint invalid; "
                    f"rebuilding: {exc}"
                )

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
            "title": clean(source_part.get("title")) or f"Part {part_number}",
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
            "scene_id": f"part_{part_number:02d}_scene_{scenes_expected:02d}",
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

    for scene in all_scenes:
        if scene["global_scene_number"] < 1:
            raise RuntimeError(
                f"Invalid global scene number: {scene['scene_id']}."
            )
        if scene.get("api_calls") != 0:
            raise RuntimeError(
                f"Unexpected API-call marker: {scene['scene_id']}."
            )

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
    log("OFFLINE STORY VISUAL MAPPING: SUCCESS")
    log("Format:", format_type)
    log("Parts:", parts_expected)
    log("Scenes per part:", scenes_expected)
    log("Total scenes:", total)
    log("Reused scenes from matching checkpoints:", reused_count)
    log("API calls: 0")
    log("Output:", OUTPUT_FILE)
    log("=" * 60)


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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
