#!/usr/bin/env python3
"""Offline story builder with topic fallback, validation and resumability."""

from __future__ import annotations

import hashlib
import json
import re
import sys
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

VERSION = "offline-story-builder-v3"


def log(*items):
    print(*items, flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


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


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def word_count(value):
    return len(re.findall(r"\S+", clean(value)))


def clean_title(value):
    value = clean(value).strip("'\"")
    value = re.sub(
        r"^(title|शीर्षक)\s*:\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return clean(value)


def as_int(value, label):
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be an integer.") from exc


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


def validate_plan(plan, format_type, topic, parts_count, scenes_per_part):
    if not isinstance(plan, dict):
        raise RuntimeError(
            "Story plan is missing or invalid. Run "
            "scripts/generate_story_plan.py first."
        )

    if clean(plan.get("format")).lower() != format_type:
        raise RuntimeError("Story plan format differs from Input/topic.txt.")

    plan_topic = clean(plan.get("topic"))
    if topic and plan_topic and topic != plan_topic:
        raise RuntimeError("Story plan topic differs from current input.")

    expected_total = parts_count * scenes_per_part
    expected = (parts_count, scenes_per_part, expected_total)

    actual = (
        as_int(plan.get("parts_count"), "plan.parts_count"),
        as_int(plan.get("scenes_per_part"), "plan.scenes_per_part"),
        as_int(plan.get("total_scenes"), "plan.total_scenes"),
    )
    if actual != expected:
        raise RuntimeError(
            f"Story plan counts {actual} do not match input {expected}. "
            "Regenerate the story plan."
        )

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan has an invalid parts list.")

    global_number = 0
    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Plan part {p} is not an object.")
        if as_int(part.get("part"), f"plan part {p}.part") != p:
            raise RuntimeError(f"Plan part numbering error at part {p}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Plan part {p} must have {scenes_per_part} scenes."
            )

        for s, scene in enumerate(scenes, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid plan scene {p}:{s}.")

            expected_id = f"part_{p:02d}_scene_{s:02d}"
            if clean(scene.get("scene_id")) != expected_id:
                raise RuntimeError(f"Plan scene ID mismatch: {expected_id}.")
            if as_int(scene.get("scene"), expected_id + ".scene") != s:
                raise RuntimeError(f"Plan scene number mismatch: {expected_id}.")
            if as_int(
                scene.get("global_scene_number"),
                expected_id + ".global_scene_number",
            ) != global_number:
                raise RuntimeError(f"Plan global number mismatch: {expected_id}.")

    return plan


def make_hook(format_type):
    if format_type == "short":
        return "उस रात जो हुआ, उसकी किसी ने कल्पना तक नहीं की थी।"
    return (
        "उस रात सामने आने वाला रहस्य हर जिंदगी और हर रिश्ते को "
        "हमेशा के लिए बदल देने वाला था।"
    )


def split_text(text, count):
    """Distribute supplied text in order without external API calls."""
    text = re.sub(r"\r\n?", "\n", str(text or "")).strip()
    units = [
        clean(x)
        for x in re.split(r"(?<=[।.!?])\s+|\n+", text)
        if clean(x)
    ]
    if not units:
        return [""] * count

    if len(units) >= count:
        return [
            " ".join(
                units[
                    round(i * len(units) / count):
                    round((i + 1) * len(units) / count)
                ]
            ).strip()
            for i in range(count)
        ]

    words = re.findall(r"\S+", " ".join(units))
    if len(words) >= count:
        return [
            " ".join(
                words[
                    round(i * len(words) / count):
                    round((i + 1) * len(words) / count)
                ]
            ).strip()
            for i in range(count)
        ]

    # Do not silently invent plot details from insufficient supplied prose.
    raise RuntimeError(
        f"STORY_TEXT has only {len(words)} words for {count} scenes. "
        "Provide more story text, or leave STORY_TEXT empty to use the "
        "explicitly labelled offline topic-story template."
    )


def topic_story_beats(topic, count):
    """Create a transparent generic story skeleton from a topic, offline."""
    subject = clean(topic)
    if not subject:
        raise RuntimeError("TOPIC is empty; cannot create a topic story.")

    beats = [
        f"{subject} से जुड़ी एक असामान्य घटना ने सबका ध्यान खींचा।",
        f"शुरुआत में किसी को समझ नहीं आया कि {subject} के पीछे क्या कारण था।",
        "मुख्य पात्र ने घटनाओं को ध्यान से देखना और सवाल पूछना शुरू किया।",
        "पहला सुराग मिला, लेकिन उससे स्थिति और उलझ गई।",
        "कुछ लोगों ने चेतावनी दी कि इस मामले में आगे बढ़ना ठीक नहीं होगा।",
        "फिर एक ऐसी जानकारी सामने आई जिसने पुरानी धारणा बदल दी।",
        "मुख्य पात्र ने जोखिम उठाकर सच्चाई तक पहुँचने का फैसला किया।",
        "रास्ते में एक भरोसेमंद साथी की मदद मिली, पर भरोसे की परीक्षा भी हुई।",
        "दूसरा सुराग पहले सुराग से मेल नहीं खाता था।",
        "जाँच के दौरान एक छिपा हुआ संबंध सामने आया।",
        "अब समय कम था और गलत फैसला सब कुछ बिगाड़ सकता था।",
        "मुख्य पात्र ने तथ्यों को जोड़कर एक नई संभावना पहचानी।",
        "सबसे अहम सबूत ठीक उसी समय गायब हो गया।",
        "एक अप्रत्याशित व्यक्ति ने घटना के बारे में नई बात बताई।",
        "उस बयान की पुष्टि करने पर कहानी का रुख बदल गया।",
        "मुख्य पात्र को तय करना था कि किस पर भरोसा किया जाए।",
        "एक अंतिम कोशिश ने छिपे हुए सच तक पहुँचने का रास्ता खोला।",
        "सामना होने पर विरोधी पक्ष की बातों में बड़ा विरोधाभास मिला।",
        "अब तक के संकेत एक ही निष्कर्ष की ओर इशारा कर रहे थे।",
        "आखिरी खुलासे ने घटना का असली कारण स्पष्ट कर दिया।",
    ]

    if count <= len(beats):
        return beats[:count]

    # Expand the outline transparently, not by repeating the same scene.
    result = list(beats)
    while len(result) < count:
        n = len(result) + 1
        result.append(
            f"{subject} की जाँच में अगला चरण सामने आया। "
            f"पात्र ने उपलब्ध संकेतों की दोबारा पड़ताल की और "
            f"चरण {n} में अपनी योजना को नए सबूतों के अनुसार बदला।"
        )
    return result


def make_story(
    config, plan, format_type, title, topic, story_text,
    parts_count, scenes_per_part,
):
    total = parts_count * scenes_per_part
    if clean(story_text):
        chunks = split_text(story_text, total)
        story_type = "user_supplied_story_offline_mapping"
    else:
        chunks = topic_story_beats(topic or title, total)
        story_type = "offline_topic_template_not_ai_generated"

    hook = make_hook(format_type)
    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    final_resolution = bool_value(config, "FINAL_RESOLUTION", True)

    parts = []
    global_number = 0

    for p, plan_part in enumerate(plan["parts"], 1):
        scenes = []

        for s, plan_scene in enumerate(plan_part["scenes"], 1):
            global_number += 1
            scene_id = f"part_{p:02d}_scene_{s:02d}"
            narration = clean(chunks[global_number - 1])

            if p == 1 and s == 1:
                narration = f"{hook} {narration}".strip()
            elif s == 1 and part_hook:
                narration = f"एक नया मोड़ सामने आया। {narration}".strip()

            visual = clean(
                plan_scene.get("visual")
                or plan_scene.get("visual_prompt")
            )
            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene showing: "
                    + narration[:450]
                    + ". Natural expressions, realistic lighting, "
                    "consistent characters, no cartoon style."
                )

            purpose = clean(plan_scene.get("purpose"))
            if not purpose:
                purpose = "कहानी को आगे बढ़ाना"

            suspense = ""
            is_ending = s == scenes_per_part
            is_final = p == parts_count

            if is_ending and is_final and final_resolution:
                suspense = (
                    "आखिरकार मुख्य संघर्ष का परिणाम सामने आया और "
                    "कहानी के केंद्रीय प्रश्न का उत्तर मिल गया।"
                )
            elif is_ending and part_suspense:
                suspense = "लेकिन सबसे बड़ा रहस्य अभी सामने आना बाकी था।"

            scenes.append({
                "part": p,
                "scene": s,
                "scene_id": scene_id,
                "global_scene_number": global_number,
                "purpose": purpose,
                "narration": narration,
                "visual": visual,
                "dialogue": clean(plan_scene.get("dialogue")),
                "suspense": suspense,
            })

        parts.append({
            "part": p,
            "part_id": f"part_{p:02d}",
            "title": clean(plan_part.get("title")) or f"भाग {p}",
            "scenes": scenes,
        })

    return {
        "status": "completed",
        "format": format_type,
        "title": title,
        "topic": topic,
        "story_type": story_type,
        "hook": hook,
        "ending_type": "resolution" if final_resolution else "suspense",
        "parts": parts,
        "source_text_sha256": hashlib.sha256(
            clean(story_text).encode("utf-8")
        ).hexdigest(),
        "generation_method": "offline_no_api",
        "mapping_version": 4,
        "_generation": {
            "script_version": VERSION,
            "format": format_type,
            "parts": parts_count,
            "scenes_per_part": scenes_per_part,
            "total_scenes": total,
            "mapping_version": 4,
            "opening_hook_required": True,
            "part_hook_required": part_hook,
            "part_suspense_required": part_suspense,
            "final_resolution_requested": final_resolution,
            "scene_order": "part_then_scene",
            "api_calls": 0,
        },
    }


def validate_story(data, format_type, title, topic, parts_count, scenes_per_part):
    if not isinstance(data, dict):
        raise RuntimeError("Story root must be a JSON object.")
    if data.get("status") != "completed":
        raise RuntimeError("Story status must be completed.")
    if clean(data.get("format")).lower() != format_type:
        raise RuntimeError("Story format mismatch.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(f"Expected exactly {parts_count} parts.")

    hook = clean(data.get("hook"))
    if word_count(hook) < (7 if format_type == "short" else 14):
        raise RuntimeError("Opening hook is shorter than the configured minimum.")

    seen = set()
    global_number = 0
    first_narration = ""

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part {p}.")
        if as_int(part.get("part"), f"part {p}") != p:
            raise RuntimeError(f"Incorrect part numbering at {p}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(
                f"Part {p} must contain exactly {scenes_per_part} scenes."
            )

        part["part_id"] = f"part_{p:02d}"
        part["title"] = clean(part.get("title")) or f"भाग {p}"

        for s, scene in enumerate(scenes, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid scene {p}:{s}.")

            if as_int(scene.get("scene"), f"scene {p}:{s}") != s:
                raise RuntimeError(f"Scene numbering error at {p}:{s}.")

            scene_id = f"part_{p:02d}_scene_{s:02d}"
            scene["part"] = p
            scene["scene_id"] = scene_id
            scene["global_scene_number"] = global_number

            for field in ("purpose", "narration", "visual", "dialogue", "suspense"):
                value = scene.get(field, "")
                if not isinstance(value, str):
                    raise RuntimeError(f"{scene_id}: {field} must be text.")
                scene[field] = value.strip()

            if not scene["narration"]:
                raise RuntimeError(f"{scene_id}: narration is empty.")
            if not scene["visual"]:
                raise RuntimeError(f"{scene_id}: visual is empty.")
            if scene_id in seen:
                raise RuntimeError(f"Duplicate scene ID: {scene_id}.")
            seen.add(scene_id)

            if p == 1 and s == 1:
                first_narration = scene["narration"]

    if len(seen) != parts_count * scenes_per_part:
        raise RuntimeError("Total scene mapping validation failed.")

    if not first_narration.startswith(hook):
        raise RuntimeError("Scene 1 narration must start with the opening hook.")

    data["title"] = title
    data["topic"] = topic
    data["status"] = "completed"
    return data


def main():
    log("=" * 60)
    log("KATHA LOK AI — OFFLINE STORY BUILDER")
    log("External API calls: 0")
    log("=" * 60)

    config = load_and_validate()
    format_type = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    story_text = clean(get_story_text(config))
    parts_count = as_int(get_parts(config), "PARTS")
    scenes_per_part = as_int(get_scenes(config), "SCENES")

    if format_type not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")
    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")
    if not topic and not story_text:
        raise RuntimeError("Provide TOPIC or STORY_TEXT in Input/topic.txt.")

    title = clean_title(topic or story_text.splitlines()[0][:100])
    if not title:
        raise RuntimeError("Could not determine a title.")

    plan = validate_plan(
        read_json(PLAN_FILE),
        format_type,
        topic,
        parts_count,
        scenes_per_part,
    )

    settings = {
        key: config.get(key)
        for key in (
            "AUDIENCE", "STORY_LENGTH", "SCENE_DURATION",
            "PART_HOOK", "PART_SUSPENSE", "FINAL_RESOLUTION",
        )
    }
    fingerprint = digest({
        "version": VERSION,
        "format": format_type,
        "topic": topic,
        "story_text": story_text,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "plan": plan,
        "settings": settings,
    })

    log("Format:", format_type)
    log("Title:", title)
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Total scenes:", parts_count * scenes_per_part)
    log("Source:", "STORY_TEXT" if story_text else "offline topic template")

    try:
        saved_fingerprint = FINGERPRINT_FILE.read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        saved_fingerprint = ""

    if saved_fingerprint == fingerprint:
        saved = read_json(OUTPUT_FILE)
        if isinstance(saved, dict):
            try:
                validate_story(
                    saved, format_type, title, topic,
                    parts_count, scenes_per_part,
                )
                log("RESUME: matching validated story reused.")
                log("API calls: 0")
                return
            except (RuntimeError, TypeError, ValueError) as exc:
                log("Saved checkpoint invalid; rebuilding:", exc)

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

    log("STORY BUILD: SUCCESS")
    log("Output:", OUTPUT_FILE.relative_to(ROOT))
    log("Story type:", data["story_type"])
    log("API calls: 0")
    if not story_text:
        log(
            "WARNING: topic-only mode creates a generic offline story "
            "outline, not a fully AI-written, topic-specific narrative."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Story build cancelled.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)