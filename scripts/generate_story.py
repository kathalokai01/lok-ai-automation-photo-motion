
#!/usr/bin/env python3
"""Offline story builder with story-derived titles, natural scene splitting,
continuity-safe hooks, suspense, validation, and resumable checkpoints.
"""

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

VERSION = "offline-story-builder-v4-story-aware"


def log(*items):
    print(*items, flush=True)


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
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
    value = clean(value).strip("'\"“”‘’")
    value = re.sub(
        r"^(title|शीर्षक)\s*:\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return clean(value).strip(" .!?:;—-")


def as_int(value, label):
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be an integer.") from exc


def bool_value(config, key, default=True):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "on", "1", "enabled"}:
        return True
    if normalized in {"false", "no", "off", "0", "disabled", ""}:
        return False
    return default


def sentences(text):
    text = re.sub(r"\r\n?", "\n", str(text or "")).strip()
    result = []
    for block in re.split(r"\n+", text):
        for item in re.split(r"(?<=[।.!?])\s+", block.strip()):
            item = clean(item)
            if item:
                result.append(item)
    return result


def derive_title(topic, story_text):
    """Prefer explicit topic; otherwise derive a compact title from story."""
    explicit = clean_title(topic)
    if explicit:
        return explicit[:100]

    units = sentences(story_text)
    if not units:
        return ""

    # Avoid using the first line blindly. Prefer a sentence that contains
    # a distinctive plot clue, reversal, mystery, or decisive event.
    markers = (
        "रहस्य", "सच", "गायब", "लापता", "अचानक", "लेकिन", "मगर",
        "खुलासा", "राज", "धोखा", "आखिरी", "आखिर", "क्यों",
        "कैसे", "मौत", "चिट्ठी", "सबूत", "दरवाजा", "आवाज",
        "secret", "mystery", "missing", "suddenly", "truth",
        "betrayal", "letter", "disappeared", "revealed",
    )

    def score(sentence):
        lowered = sentence.casefold()
        marker_score = sum(2 for marker in markers if marker in lowered)
        length_score = min(len(sentence), 150) / 50
        return marker_score + length_score

    candidates = sorted(
        enumerate(units),
        key=lambda pair: (score(pair[1]), -pair[0]),
        reverse=True,
    )
    chosen = candidates[0][1]
    chosen = re.sub(
        r"^(और|लेकिन|मगर|फिर|तभी|क्योंकि|इसलिए)\s+",
        "",
        chosen,
        flags=re.IGNORECASE,
    )
    chosen = clean_title(chosen)

    # Titles should be compact, not a full plot summary.
    words = chosen.split()
    if len(words) > 9:
        chosen = " ".join(words[:9]).rstrip(" ,;:-") + "…"
    if len(chosen) > 90:
        chosen = chosen[:87].rsplit(" ", 1)[0] + "…"

    return chosen or "Katha Lok AI कहानी"


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
    actual = (
        as_int(plan.get("parts_count"), "plan.parts_count"),
        as_int(plan.get("scenes_per_part"), "plan.scenes_per_part"),
        as_int(plan.get("total_scenes"), "plan.total_scenes"),
    )
    expected = (parts_count, scenes_per_part, expected_total)

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

        scene_rows = part.get("scenes")
        if (
            not isinstance(scene_rows, list)
            or len(scene_rows) != scenes_per_part
        ):
            raise RuntimeError(
                f"Plan part {p} must have {scenes_per_part} scenes."
            )

        for s, scene in enumerate(scene_rows, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid plan scene {p}:{s}.")

            expected_id = f"part_{p:02d}_scene_{s:02d}"
            if clean(scene.get("scene_id")) != expected_id:
                raise RuntimeError(f"Plan scene ID mismatch: {expected_id}.")
            if as_int(scene.get("scene"), expected_id + ".scene") != s:
                raise RuntimeError(
                    f"Plan scene number mismatch: {expected_id}."
                )
            if as_int(
                scene.get("global_scene_number"),
                expected_id + ".global_scene_number",
            ) != global_number:
                raise RuntimeError(
                    f"Plan global number mismatch: {expected_id}."
                )

    return plan


def split_text(text, count):
    """Keep sentence boundaries where possible; never reorder the source."""
    units = sentences(text)
    if not units:
        raise RuntimeError("STORY_TEXT contains no usable sentences.")

    if len(units) < count:
        total_words = word_count(" ".join(units))
        if total_words < count:
            raise RuntimeError(
                f"STORY_TEXT has only {total_words} words for {count} scenes. "
                "Provide more story text or reduce SCENES/PARTS."
            )

        # If there are fewer sentences than scenes, split at word boundaries.
        words = " ".join(units).split()
        result = []
        for index in range(count):
            start = round(index * len(words) / count)
            end = round((index + 1) * len(words) / count)
            result.append(" ".join(words[start:end]).strip())
        return result

    # Greedy contiguous sentence grouping, balanced by word count.
    total_words = sum(word_count(item) for item in units)
    result = []
    cursor = 0

    for group_index in range(count):
        remaining_groups = count - group_index
        remaining_units = len(units) - cursor

        if remaining_groups == 1:
            result.append(" ".join(units[cursor:]).strip())
            break

        target = sum(word_count(x) for x in units[cursor:]) / remaining_groups
        group = []
        group_words = 0
        max_take = remaining_units - (remaining_groups - 1)

        while cursor < len(units) and len(group) < max_take:
            candidate = units[cursor]
            candidate_words = word_count(candidate)

            if group and abs(group_words - target) <= abs(
                group_words + candidate_words - target
            ):
                break

            group.append(candidate)
            group_words += candidate_words
            cursor += 1

            if group_words >= target:
                break

        if not group and cursor < len(units):
            group.append(units[cursor])
            cursor += 1

        result.append(" ".join(group).strip())

    if len(result) != count or any(not item for item in result):
        raise RuntimeError("Could not split story into the required scenes.")

    return result


def topic_story_beats(topic, count):
    """Transparent offline outline; this is not falsely labelled AI-written."""
    subject = clean(topic)
    if not subject:
        raise RuntimeError("TOPIC is empty; cannot create a topic story.")

    beats = [
        f"{subject} से जुड़ी एक असामान्य घटना ने सबका ध्यान खींचा।",
        f"शुरुआत में किसी को समझ नहीं आया कि {subject} के पीछे क्या कारण था।",
        "मुख्य पात्र ने घटनाओं को ध्यान से देखा और पहला सवाल पूछा।",
        "पहला सुराग मिला, लेकिन उससे मामला और उलझ गया।",
        "एक चेतावनी ने मुख्य पात्र को आगे बढ़ने से रोकने की कोशिश की।",
        "नई जानकारी ने पहले से बनी धारणा को बदल दिया।",
        "मुख्य पात्र ने जोखिम उठाकर सच्चाई खोजने का फैसला किया।",
        "एक भरोसेमंद साथी की मदद मिली, पर भरोसे की परीक्षा भी हुई।",
        "दूसरा सुराग पहले सुराग से मेल नहीं खाता था।",
        "जाँच में एक छिपा हुआ संबंध सामने आया।",
        "समय कम था और गलत फैसला सब कुछ बिगाड़ सकता था।",
        "मुख्य पात्र ने संकेतों को जोड़कर नई संभावना पहचानी।",
        "सबसे अहम सबूत ठीक उसी समय गायब हो गया।",
        "एक अप्रत्याशित व्यक्ति ने घटना के बारे में नई बात बताई।",
        "उस बयान की जाँच से कहानी का रुख बदल गया।",
        "मुख्य पात्र को तय करना था कि किस पर भरोसा किया जाए।",
        "एक अंतिम कोशिश ने सच्चाई तक पहुँचने का रास्ता खोला।",
        "सामने आने पर विरोधी पक्ष की बातों में विरोधाभास मिला।",
        "सभी संकेत एक ही निष्कर्ष की ओर इशारा कर रहे थे।",
        "आखिरी खुलासे ने घटना का असली कारण स्पष्ट कर दिया।",
    ]

    result = list(beats[:count])
    while len(result) < count:
        number = len(result) + 1
        result.append(
            f"{subject} की जाँच में अगला चरण सामने आया। "
            f"मुख्य पात्र ने उपलब्ध संकेतों की पड़ताल की और "
            f"चरण {number} में अपनी योजना बदली।"
        )
    return result


def story_hook(topic, story_text, first_chunk, format_type):
    """Build an opening line using the supplied story/topic when available."""
    source = clean(story_text)
    units = sentences(source)

    if source and units:
        markers = (
            "रहस्य", "गायब", "लापता", "अचानक", "लेकिन", "मगर",
            "सच", "राज", "खुलासा", "क्यों", "कैसे", "सबूत",
            "मौत", "आवाज", "चिट्ठी", "दरवाजा",
        )
        candidates = [
            unit for unit in units
            if any(marker in unit.casefold() for marker in markers)
        ]
        if candidates:
            clue = candidates[0]
            if word_count(clue) > 22:
                clue = " ".join(clue.split()[:22]).rstrip(" ,;:-") + "…"
            return f"इस कहानी का सबसे अहम सवाल यहीं से शुरू होता है: {clue}"

        topic_text = clean(topic)
        if topic_text:
            return (
                f"{topic_text} की कहानी में एक ऐसी बात सामने आती है "
                "जिसका अर्थ अंत तक बदलता रहता है।"
            )

        clue = units[-1] if len(units) > 1 else units[0]
        if word_count(clue) > 18:
            clue = " ".join(clue.split()[:18]).rstrip(" ,;:-") + "…"
        return f"इस कहानी का सुराग शुरुआत में ही छिपा है: {clue}"

    subject = clean(topic) or clean(first_chunk)
    if word_count(subject) > 14:
        subject = " ".join(subject.split()[:14])
    if format_type == "short":
        return (
            f"{subject} से जुड़ी एक बात सब कुछ बदल सकती थी—"
            "लेकिन उसका असली मतलब अभी साफ नहीं था।"
        )
    return (
        f"{subject} की शुरुआत में दिखाई देने वाली बात मामूली लगती थी, "
        "लेकिन उसके पीछे छिपा सच हर फैसले को बदल सकता था।"
    )


def part_suspense(text, topic):
    """Use an actual clue from the part instead of the same canned ending."""
    units = sentences(text)
    subject = clean(topic) or "इस कहानी"

    if not units:
        return (
            f"{subject} का सबसे अहम सवाल अभी अनसुलझा है।"
        )

    ending = units[-1].strip()
    if word_count(ending) > 24:
        ending = " ".join(ending.split()[-18:])

    lowered = ending.casefold()
    question_words = ("क्यों", "कैसे", "कौन", "कहाँ", "क्या", "?")
    if any(word in lowered for word in question_words):
        return f"अब सवाल है: {ending}"

    return (
        f"{ending} — लेकिन इससे जुड़ा अगला सवाल अभी बाकी है।"
    )


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

    hook = story_hook(topic or title, story_text, chunks[0], format_type)
    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    final_resolution = bool_value(config, "FINAL_RESOLUTION", True)

    parts = []
    global_number = 0

    for p, plan_part in enumerate(plan["parts"], 1):
        scenes = []
        part_chunks = []

        for s, plan_scene in enumerate(plan_part["scenes"], 1):
            global_number += 1
            narration = clean(chunks[global_number - 1])
            part_chunks.append(narration)

            if p == 1 and s == 1:
                narration = f"{hook} {narration}".strip()
            elif s == 1 and part_hook:
                prior_part_end = ""
                if parts:
                    prior_part_end = clean(
                        parts[-1]["scenes"][-1].get("narration")
                    )
                if prior_part_end:
                    narration = (
                        f"पिछले भाग की घटना के बाद, "
                        f"{narration}"
                    ).strip()

            visual = clean(
                plan_scene.get("visual")
                or plan_scene.get("visual_prompt")
            )
            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene showing: "
                    + narration[:450]
                    + ". Natural expressions, realistic lighting, "
                    "consistent character appearance and clothing, "
                    "no cartoon style, no exaggerated contrast."
                )

            purpose = clean(plan_scene.get("purpose")) or "कहानी को आगे बढ़ाना"
            suspense = ""
            is_ending = s == scenes_per_part
            is_final = p == parts_count

            if is_ending and is_final and final_resolution:
                ending_source = clean(" ".join(part_chunks))
                suspense = (
                    f"कहानी का केंद्रीय प्रश्न अब अपने परिणाम तक पहुँचता है: "
                    f"{ending_source[-240:]}"
                )
            elif is_ending and part_suspense:
                suspense = part_suspense_text(
                    " ".join(part_chunks), topic or title
                )

            scenes.append({
                "part": p,
                "scene": s,
                "scene_id": f"part_{p:02d}_scene_{s:02d}",
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
        "topic": topic or title,
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


def part_suspense_text(text, topic):
    return part_suspense(text, topic)


def validate_story(
    data, format_type, title, topic, parts_count, scenes_per_part
):
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
    if word_count(hook) < 7:
        raise RuntimeError("Opening hook is shorter than the minimum.")

    seen = set()
    global_number = 0
    first_narration = ""

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid story part {p}.")
        if as_int(part.get("part"), f"part {p}") != p:
            raise RuntimeError(f"Incorrect part numbering at {p}.")

        scene_rows = part.get("scenes")
        if (
            not isinstance(scene_rows, list)
            or len(scene_rows) != scenes_per_part
        ):
            raise RuntimeError(
                f"Part {p} must contain exactly {scenes_per_part} scenes."
            )

        part["part_id"] = f"part_{p:02d}"
        part["title"] = clean(part.get("title")) or f"भाग {p}"

        for s, scene in enumerate(scene_rows, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid scene {p}:{s}.")
            if as_int(scene.get("scene"), f"scene {p}:{s}") != s:
                raise RuntimeError(f"Scene numbering error at {p}:{s}.")

            scene_id = f"part_{p:02d}_scene_{s:02d}"
            scene["part"] = p
            scene["scene_id"] = scene_id
            scene["global_scene_number"] = global_number

            for field in (
                "purpose", "narration", "visual", "dialogue", "suspense"
            ):
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
    data["topic"] = topic or title
    data["status"] = "completed"
    return data


def main():
    log("=" * 60)
    log("KATHA LOK AI — STORY BUILDER")
    log("External API calls: 0")
    log("=" * 60)

    config = load_and_validate(ROOT / "Input" / "topic.txt")
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

    title = derive_title(topic, story_text)
    if not title:
        raise RuntimeError("Could not determine a story title.")

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
        "title": title,
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
    if not story_text:
        log(
            "WARNING: topic-only mode uses a generic offline story outline; "
            "it is not a fully AI-written, story-specific narrative."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Story build cancelled.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
