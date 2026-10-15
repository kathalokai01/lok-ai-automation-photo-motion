
#!/usr/bin/env python3
"""Build a validated, resumable story.json without paid/API generation.

When STORY_TEXT is supplied, this script deterministically maps it onto the
existing story plan. It deliberately makes zero Gemini/API requests.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from input_config import (
    load_and_validate,
    get_format,
    get_topic,
    get_story_text,
    get_parts,
    get_scenes,
)

ROOT = Path(__file__).resolve().parent.parent
STORY_DIR = ROOT / "output" / "story"
PLAN_FILE = STORY_DIR / "story_plan.json"
OUTPUT_FILE = STORY_DIR / "story.json"
TITLE_FILE = STORY_DIR / "final_title.txt"
FINGERPRINT_FILE = STORY_DIR / "story.fingerprint"

VERSION = "offline-story-builder-v1"


def log(*items):
    print(*items, flush=True)


def read_json(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def write_json(path, data):
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def digest(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_text(value):
    value = str(value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def word_count(value):
    return len(normalize_text(value).split())


def clean_title(value):
    value = str(value or "").strip()
    value = re.sub(r"^['\"]+|['\"]+$", "", value)
    value = re.sub(
        r"^(title|शीर्षक)\s*:\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", value).strip()


def bool_value(config, key, default=True):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"true", "yes", "on", "1"}:
        return True
    if value in {"false", "no", "off", "0", ""}:
        return False
    return default


def validate_plan(plan, format_type, topic, parts_count, scenes_per_part):
    if not isinstance(plan, dict):
        raise RuntimeError(
            "Story plan missing/invalid. Run scripts/generate_story_plan.py first."
        )

    if str(plan.get("format", "")).strip().lower() != format_type:
        raise RuntimeError("Story plan format does not match Input/topic.txt.")

    plan_topic = str(plan.get("topic", "")).strip()
    if topic and plan_topic and topic != plan_topic:
        raise RuntimeError("Story plan topic does not match current input.")

    expected_total = parts_count * scenes_per_part
    try:
        actual = (
            int(plan.get("parts_count", -1)),
            int(plan.get("scenes_per_part", -1)),
            int(plan.get("total_scenes", -1)),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Story plan scene counts are invalid.") from exc

    expected = (parts_count, scenes_per_part, expected_total)
    if actual != expected:
        raise RuntimeError(
            f"Story plan counts {actual} do not match input {expected}. "
            "Regenerate the story plan before continuing."
        )

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan has an invalid parts list.")

    global_number = 0
    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid plan part {part_number}.")
        if int(part.get("part", -1)) != part_number:
            raise RuntimeError(f"Incorrect plan part number: {part_number}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Plan part {part_number} must have {scenes_per_part} scenes."
            )

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError("Plan contains an invalid scene.")
            expected_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
            try:
                actual_scene = int(scene.get("scene", -1))
                actual_global = int(scene.get("global_scene_number", -1))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Invalid plan scene numbering.") from exc

            if (
                actual_scene != scene_number
                or actual_global != global_number
                or scene.get("scene_id") != expected_id
            ):
                raise RuntimeError(
                    f"Scene mapping mismatch at {expected_id}. "
                    "Regenerate the story plan."
                )
    return plan


def split_story_into_units(text):
    """Split supplied prose into sentence-like units, retaining the wording."""
    text = re.sub(r"\r\n?", "\n", str(text or "")).strip()
    text = re.sub(r"[ \t]+", " ", text)
    if not text:
        return []

    units = re.split(r"(?<=[।.!?])\s+|\n+", text)
    return [re.sub(r"\s+", " ", item).strip() for item in units if item.strip()]


def distribute_text(text, total_scenes):
    """Spread existing story text across scenes without any external API call."""
    units = split_story_into_units(text)
    if not units:
        raise RuntimeError("STORY_TEXT is empty after cleanup.")

    if len(units) >= total_scenes:
        # Keep units in their original order; group adjacent sentences evenly.
        result = []
        for i in range(total_scenes):
            start = round(i * len(units) / total_scenes)
            end = round((i + 1) * len(units) / total_scenes)
            result.append(" ".join(units[start:end]).strip())
        return result

    # If the story has fewer sentences than scenes, divide the original words
    # into ordered contiguous chunks rather than silently dropping sentences.
    words = re.findall(r"\S+", " ".join(units))
    if len(words) >= total_scenes:
        result = []
        for i in range(total_scenes):
            start = round(i * len(words) / total_scenes)
            end = round((i + 1) * len(words) / total_scenes)
            result.append(" ".join(words[start:end]).strip())
        return result

    # Very short input cannot provide distinct narration for every scene.
    # Preserve all original text and clearly label the limitation in metadata.
    result = []
    for i in range(total_scenes):
        result.append(units[i % len(units)])
    return result


def make_hook(format_type):
    if format_type == "short":
        return "उस रात जो हुआ, उसकी किसी ने कल्पना तक नहीं की थी।"
    return (
        "उस रात सामने आने वाला रहस्य हर जिंदगी और हर रिश्ते को "
        "हमेशा के लिए बदल देने वाला था।"
    )


def make_story(
    config, plan, format_type, title, topic, story_text,
    parts_count, scenes_per_part,
):
    total = parts_count * scenes_per_part
    chunks = distribute_text(story_text, total)
    hook = make_hook(format_type)

    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    final_resolution = bool_value(config, "FINAL_RESOLUTION", True)

    result_parts = []
    global_number = 0

    for part_number, plan_part in enumerate(plan["parts"], 1):
        result_scenes = []
        plan_scenes = plan_part["scenes"]

        for scene_number, plan_scene in enumerate(plan_scenes, 1):
            global_number += 1
            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
            narration = chunks[global_number - 1].strip()

            # Ensure Scene 1 starts with the exact top-level hook.
            if part_number == 1 and scene_number == 1:
                narration = f"{hook} {narration}".strip()
            elif scene_number == 1 and part_hook:
                narration = f"एक नया मोड़ सामने आया। {narration}".strip()

            if not narration:
                narration = (
                    f"{title} की कहानी में आगे बढ़ते हुए घटनाएँ "
                    f"एक नया मोड़ लेती हैं।"
                )

            suspense = ""
            if scene_number == scenes_per_part and part_suspense:
                suspense = (
                    "लेकिन सबसे बड़ा रहस्य अभी सामने आना बाकी था।"
                )
            elif (
                part_number == parts_count
                and scene_number == scenes_per_part
                and final_resolution
            ):
                suspense = "आखिरकार कहानी का मुख्य संघर्ष अपने परिणाम तक पहुँचा।"

            source_visual = str(
                plan_scene.get("visual")
                or plan_scene.get("visual_prompt")
                or ""
            ).strip()
            visual = source_visual or (
                f"Realistic live-action cinematic scene illustrating: "
                f"{narration[:450]}. Natural expressions, believable setting, "
                "consistent characters, realistic lighting, no cartoon look."
            )

            purpose = str(plan_scene.get("purpose") or "").strip()
            if not purpose:
                purpose = (
                    "कहानी को आगे बढ़ाना और इस दृश्य के घटनाक्रम को स्पष्ट करना"
                )

            dialogue = str(plan_scene.get("dialogue") or "").strip()

            result_scenes.append({
                "scene": scene_number,
                "scene_id": scene_id,
                "global_scene_number": global_number,
                "purpose": purpose,
                "narration": narration,
                "visual": visual,
                "dialogue": dialogue,
                "suspense": suspense,
            })

        result_parts.append({
            "part": part_number,
            "part_id": f"part_{part_number:02d}",
            "title": str(
                plan_part.get("title") or f"भाग {part_number}"
            ).strip(),
            "scenes": result_scenes,
        })

    return {
        "status": "completed",
        "format": format_type,
        "title": title,
        "topic": topic,
        "story_type": "user_supplied_story_offline_mapping",
        "hook": hook,
        "ending_type": "offline_mapped_story",
        "parts": result_parts,
        "source_text_sha256": hashlib.sha256(
            story_text.encode("utf-8")
        ).hexdigest(),
        "generation_method": "offline_no_api",
        "mapping_version": 3,
        "_generation": {
            "script_version": VERSION,
            "format": format_type,
            "parts": parts_count,
            "scenes_per_part": scenes_per_part,
            "total_scenes": total,
            "mapping_version": 3,
            "opening_hook_required": True,
            "part_hook_required": part_hook,
            "part_suspense_required": part_suspense,
            "final_resolution_requested": final_resolution,
            "scene_order": "part_then_scene",
            "api_calls": 0,
        },
    }


def validate_story(
    data, format_type, title, topic, parts_count, scenes_per_part,
):
    if not isinstance(data, dict):
        raise RuntimeError("Story must be a JSON object.")
    if data.get("status") != "completed":
        raise RuntimeError("Story status must be completed.")
    if str(data.get("format", "")).lower() != format_type:
        raise RuntimeError("Story format mismatch.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(f"Expected exactly {parts_count} parts.")

    hook = str(data.get("hook") or "").strip()
    if word_count(hook) < (7 if format_type == "short" else 14):
        raise RuntimeError("Opening hook does not meet minimum word count.")

    seen = set()
    first_narration = ""
    global_number = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != part_number:
            raise RuntimeError(f"Invalid part numbering at {part_number}.")
        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Part {part_number} must contain {scenes_per_part} scenes."
            )

        part["part_id"] = f"part_{part_number:02d}"
        part["title"] = str(part.get("title") or f"भाग {part_number}").strip()

        for scene_number, scene in enumerate(scenes, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError("Invalid scene object.")

            if int(scene.get("scene", -1)) != scene_number:
                raise RuntimeError("Scene numbering is not sequential.")

            scene_id = f"part_{part_number:02d}_scene_{scene_number:02d}"
            scene["part"] = part_number
            scene["scene_id"] = scene_id
            scene["global_scene_number"] = global_number

            for field in ("purpose", "narration", "visual", "dialogue", "suspense"):
                value = scene.get(field, "")
                if not isinstance(value, str):
                    raise RuntimeError(f"{scene_id}: {field} must be text.")
                scene[field] = value.strip()

            if not scene["narration"] or not scene["visual"]:
                raise RuntimeError(
                    f"{scene_id}: narration and visual are required."
                )
            if scene_id in seen:
                raise RuntimeError(f"Duplicate scene ID: {scene_id}")
            seen.add(scene_id)

            if part_number == 1 and scene_number == 1:
                first_narration = scene["narration"]

    if len(seen) != parts_count * scenes_per_part:
        raise RuntimeError("Scene mapping count validation failed.")
    if not normalize_text(first_narration).startswith(normalize_text(hook)):
        raise RuntimeError("Scene 1 narration must start with the exact hook.")

    data["status"] = "completed"
    data["format"] = format_type
    data["title"] = title
    data["topic"] = topic
    return data


def main():
    log("=" * 60)
    log("KATHA LOK AI — OFFLINE STORY BUILDER")
    log("Story-generation API calls: 0")
    log("=" * 60)

    config = load_and_validate()
    format_type = get_format(config)
    topic = get_topic(config)
    story_text = get_story_text(config)
    parts_count = get_parts(config)
    scenes_per_part = get_scenes(config)

    if not story_text.strip():
        raise RuntimeError(
            "STORY_TEXT is empty. This free/offline script will not invent "
            "a full story from TOPIC alone or call Gemini. Add your complete "
            "story under STORY_TEXT, then run again."
        )

    title = clean_title(topic or story_text.splitlines()[0][:100])
    if not title:
        raise RuntimeError("Could not determine the story title.")

    plan = validate_plan(
        read_json(PLAN_FILE),
        format_type,
        topic,
        parts_count,
        scenes_per_part,
    )

    fingerprint = digest({
        "version": VERSION,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "plan": plan,
        "settings": {
            key: config.get(key)
            for key in (
                "AUDIENCE", "STORY_LENGTH", "SCENE_DURATION",
                "PART_HOOK", "PART_SUSPENSE", "FINAL_RESOLUTION",
            )
        },
    })

    log("Format:", format_type)
    log("Title:", title)
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", parts_count * scenes_per_part)
    log("Source text words:", word_count(story_text))

    saved_fingerprint = ""
    if FINGERPRINT_FILE.is_file():
        try:
            saved_fingerprint = FINGERPRINT_FILE.read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            pass

    if saved_fingerprint == fingerprint:
        saved = read_json(OUTPUT_FILE)
        if isinstance(saved, dict):
            try:
                saved = validate_story(
                    saved, format_type, title, topic,
                    parts_count, scenes_per_part,
                )
                log("RESUME: Matching completed story reused; API calls: 0")
                return
            except Exception as exc:
                log("Saved story is not reusable:", exc)

    data = make_story(
        config, plan, format_type, title, topic, story_text,
        parts_count, scenes_per_part,
    )
    data = validate_story(
        data, format_type, title, topic,
        parts_count, scenes_per_part,
    )

    write_json(OUTPUT_FILE, data)
    atomic_write(TITLE_FILE, title + "\n")
    atomic_write(FINGERPRINT_FILE, fingerprint + "\n")

    log("STORY BUILD SUCCESS")
    log("Validated output:", OUTPUT_FILE)
    log("API calls: 0")
    log("Note: This maps existing prose; it does not AI-expand or rewrite it.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Story generation cancelled.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
