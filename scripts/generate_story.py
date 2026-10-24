
#!/usr/bin/env python3
"""Schema-compatible, offline story builder for Katha Lok AI."""

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

VERSION = "story-builder-v6-short-word-aware-schema-compatible"
MIN_WORDS_PER_SHORT_PART = 120
MAX_WORDS_PER_SHORT_PART = 150
TARGET_WORDS_PER_SHORT_PART = 135


def log(*items):
    print(*items, flush=True)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def word_count(value):
    return len(re.findall(r"\S+", clean(value)))


def digest(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def sentences(text):
    """Split on sentence boundaries while preserving the source order."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    result = []
    for block in re.split(r"\n+", text):
        for item in re.split(r"(?<=[।.!?])\s+", block.strip()):
            item = clean(item)
            if item:
                result.append(item)
    return result


def clean_title(value):
    value = clean(value).strip("'\"“”‘’")
    value = re.sub(
        r"^(title|शीर्षक)\s*:\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return clean(value).strip(" .!?:;—-")


def derive_title(topic, story_text):
    if clean_title(topic):
        return clean_title(topic)[:100]

    units = sentences(story_text)
    if not units:
        return ""

    markers = (
        "रहस्य", "सच", "गायब", "लापता", "अचानक", "खुलासा",
        "राज", "धोखा", "आखिरी", "आखिर", "क्यों", "कैसे",
        "मौत", "चिट्ठी", "सबूत", "दरवाजा", "आवाज",
        "secret", "mystery", "missing", "truth", "betrayal",
        "letter", "disappeared", "revealed",
    )

    def score(item):
        lower = item.casefold()
        return (
            sum(3 for marker in markers if marker in lower)
            + min(len(item), 150) / 50
        )

    chosen = max(
        enumerate(units),
        key=lambda item: (score(item[1]), -item[0]),
    )[1]

    chosen = re.sub(
        r"^(और|लेकिन|मगर|फिर|तभी|क्योंकि|इसलिए)\s+",
        "",
        chosen,
        flags=re.IGNORECASE,
    )
    words = chosen.split()
    if len(words) > 9:
        chosen = " ".join(words[:9]).rstrip(" ,;:-") + "…"
    return clean_title(chosen)[:90] or "Katha Lok AI कहानी"


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


def split_balanced(text, count):
    """Split into exactly count non-empty chunks, keeping source order."""
    units = sentences(text)
    if not units:
        raise RuntimeError("Story text has no usable sentences.")
    if count < 1:
        raise RuntimeError("Split count must be positive.")

    if len(units) < count:
        words = " ".join(units).split()
        if len(words) < count:
            raise RuntimeError(
                f"Only {len(words)} words are available for {count} scenes. "
                "Add story text or reduce PARTS/SCENES."
            )
        chunks = []
        for i in range(count):
            start = round(i * len(words) / count)
            end = round((i + 1) * len(words) / count)
            chunks.append(" ".join(words[start:end]).strip())
        return chunks

    chunks = []
    cursor = 0
    for group_index in range(count):
        groups_left = count - group_index
        units_left = len(units) - cursor

        if groups_left == 1:
            chunks.append(" ".join(units[cursor:]).strip())
            break

        target = (
            sum(word_count(item) for item in units[cursor:])
            / groups_left
        )
        group = []
        current_words = 0
        max_take = units_left - (groups_left - 1)

        while cursor < len(units) and len(group) < max_take:
            candidate = units[cursor]
            candidate_words = word_count(candidate)

            if group and abs(current_words - target) <= abs(
                current_words + candidate_words - target
            ):
                break

            group.append(candidate)
            current_words += candidate_words
            cursor += 1

            if current_words >= target:
                break

        if not group and cursor < len(units):
            group.append(units[cursor])
            cursor += 1

        chunks.append(" ".join(group).strip())

    if len(chunks) != count or any(not item for item in chunks):
        raise RuntimeError("Could not split story into requested sections.")
    return chunks


def split_short_parts(text, parts_count):
    """
    Split at sentence boundaries, aiming for 135 words per part.
    Preserve all supplied story text; never invent missing words.
    """
    units = sentences(text)
    if not units:
        raise RuntimeError("Short story contains no usable sentences.")

    if len(units) < parts_count:
        return split_balanced(text, parts_count)

    chunks = []
    cursor = 0

    for index in range(parts_count):
        parts_left = parts_count - index
        units_left = len(units) - cursor

        if parts_left == 1:
            chunks.append(" ".join(units[cursor:]).strip())
            break

        # Keep at least one sentence for every remaining part.
        max_take = units_left - (parts_left - 1)
        group = []
        current_words = 0

        while cursor < len(units) and len(group) < max_take:
            candidate = units[cursor]
            candidate_words = word_count(candidate)

            if group:
                current_distance = abs(
                    current_words - TARGET_WORDS_PER_SHORT_PART
                )
                next_distance = abs(
                    current_words + candidate_words
                    - TARGET_WORDS_PER_SHORT_PART
                )
                # Once near the target, stop before crossing it too far.
                if current_distance <= next_distance:
                    break

            group.append(candidate)
            current_words += candidate_words
            cursor += 1

            if current_words >= TARGET_WORDS_PER_SHORT_PART:
                break

        if not group and cursor < len(units):
            group.append(units[cursor])
            cursor += 1

        chunks.append(" ".join(group).strip())

    if len(chunks) != parts_count or any(not item for item in chunks):
        raise RuntimeError("Could not create the requested short parts.")
    return chunks


def validate_plan(plan, format_type, topic, parts_count, scenes_per_part):
    if not isinstance(plan, dict):
        raise RuntimeError(
            "Story plan missing/invalid. Run generate_story_plan.py first."
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
            "Regenerate the plan before continuing."
        )

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan has an invalid parts list.")

    global_number = 0
    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict) or as_int(
            part.get("part"), f"plan part {p}"
        ) != p:
            raise RuntimeError(f"Invalid plan part numbering at {p}.")

        rows = part.get("scenes")
        if not isinstance(rows, list) or len(rows) != scenes_per_part:
            raise RuntimeError(f"Plan part {p} has the wrong scene count.")

        for s, row in enumerate(rows, 1):
            global_number += 1
            expected_id = f"part_{p:02d}_scene_{s:02d}"
            if not isinstance(row, dict):
                raise RuntimeError(f"Invalid plan scene {expected_id}.")
            if clean(row.get("scene_id")) != expected_id:
                raise RuntimeError(f"Scene ID mismatch: {expected_id}.")
            if as_int(row.get("scene"), expected_id) != s:
                raise RuntimeError(f"Local scene number mismatch: {expected_id}.")
            if as_int(row.get("global_scene_number"), expected_id) != global_number:
                raise RuntimeError(f"Global scene number mismatch: {expected_id}.")

    return plan


def topic_story_beats(topic, count):
    """Offline outline fallback; explicitly not represented as AI-written."""
    subject = clean(topic) or "इस कहानी"
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


def story_hook(topic, title, story_text, first_chunk, format_type):
    """Prefer real story wording over a repeated generic hook."""
    units = sentences(story_text)
    if units:
        first = units[0]
        if word_count(first) >= 7:
            return first
        if len(units) > 1 and word_count(units[1]) >= 7:
            return units[1]

        combined = " ".join(units[:2]).strip()
        if word_count(combined) >= 7:
            return combined

    subject = clean(topic) or clean(title) or clean(first_chunk)
    subject = subject.rstrip("।.!? ")
    if format_type == "short":
        return (
            f"{subject} से जुड़ी इस घटना में एक ऐसा सवाल छिपा है "
            "जिसका जवाब आगे की कहानी में मिलेगा।"
        )
    return (
        f"{subject} की असली वजह समझने के लिए घटनाओं के "
        "हर संकेत को ध्यान से देखना होगा।"
    )


def suspense_ending(text, topic):
    units = sentences(text)
    if not units:
        return ""

    ending = units[-1]
    if word_count(ending) > 24:
        ending = " ".join(ending.split()[-18:])

    if any(
        token in ending.casefold()
        for token in ("क्यों", "कैसे", "कौन", "कहाँ", "क्या", "?")
    ):
        return ending

    # The supplied ending remains the suspense anchor; no new plot fact is added.
    subject = clean(topic)
    if subject:
        return f"{ending} अब {subject} से जुड़ा अगला सवाल बाकी है।"
    return f"{ending} अब इससे जुड़ा अगला सवाल बाकी है।"


def make_story(
    config, plan, format_type, title, topic, story_text,
    parts_count, scenes_per_part,
):
    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    final_resolution = bool_value(config, "FINAL_RESOLUTION", True)

    if clean(story_text):
        if format_type == "short":
            part_texts = split_short_parts(story_text, parts_count)
            chunks_by_part = [
                split_balanced(part_text, scenes_per_part)
                for part_text in part_texts
            ]
            story_type = "user_supplied_story_short_part_mapping"
        else:
            all_chunks = split_balanced(
                story_text, parts_count * scenes_per_part
            )
            chunks_by_part = [
                all_chunks[i * scenes_per_part:(i + 1) * scenes_per_part]
                for i in range(parts_count)
            ]
            story_type = "user_supplied_story_offline_mapping"
    else:
        all_chunks = topic_story_beats(
            topic or title, parts_count * scenes_per_part
        )
        chunks_by_part = [
            all_chunks[i * scenes_per_part:(i + 1) * scenes_per_part]
            for i in range(parts_count)
        ]
        story_type = "offline_topic_template_not_ai_generated"

    hook = story_hook(
        topic, title, story_text, chunks_by_part[0][0], format_type
    )
    parts = []
    global_number = 0
    part_word_counts = []

    for p, plan_part in enumerate(plan["parts"], 1):
        scenes = []
        raw_chunks = chunks_by_part[p - 1]
        part_text = " ".join(raw_chunks).strip()
        part_word_counts.append(word_count(part_text))

        for s, plan_scene in enumerate(plan_part["scenes"], 1):
            global_number += 1
            narration = clean(raw_chunks[s - 1])

            if p == 1 and s == 1:
                # Avoid repeating the hook when it is already the opening text.
                if not narration.startswith(hook):
                    narration = f"{hook} {narration}".strip()
            elif s == 1 and part_hook and p > 1:
                narration = f"पिछले भाग की घटना के बाद, {narration}".strip()

            visual = clean(
                plan_scene.get("visual") or plan_scene.get("visual_prompt")
            )
            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene showing: "
                    + narration[:450]
                    + ". Natural expressions, realistic lighting, "
                    "consistent character identity and clothing, "
                    "natural exposure, no cartoon style, no exaggerated contrast."
                )

            suspense = ""
            if s == scenes_per_part:
                if p == parts_count and final_resolution:
                    # Do not falsely claim that an unresolved story was resolved.
                    suspense = (
                        "अंतिम भाग: उपलब्ध कहानी के निष्कर्ष को स्पष्ट करें; "
                        "किसी असमर्थित घटना या खुलासे को न जोड़ें।"
                    )
                elif part_suspense:
                    suspense = suspense_ending(part_text, topic or title)

            scenes.append({
                "part": p,
                "scene": s,
                "scene_id": f"part_{p:02d}_scene_{s:02d}",
                "global_scene_number": global_number,
                "purpose": clean(plan_scene.get("purpose")) or "कहानी को आगे बढ़ाना",
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
        "part_word_counts": part_word_counts,
        "short_word_target": {
            "minimum": MIN_WORDS_PER_SHORT_PART,
            "maximum": MAX_WORDS_PER_SHORT_PART,
        } if format_type == "short" else None,
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
            "total_scenes": parts_count * scenes_per_part,
            "mapping_version": 4,
            "opening_hook_required": True,
            "part_hook_required": part_hook,
            "part_suspense_required": part_suspense,
            "final_resolution_requested": final_resolution,
            "scene_order": "part_then_scene",
            "api_calls": 0,
        },
    }


def validate_story(
    data, format_type, title, topic, parts_count, scenes_per_part
):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Story root/status is invalid.")
    if clean(data.get("format")).lower() != format_type:
        raise RuntimeError("Story format mismatch.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(f"Expected exactly {parts_count} parts.")

    hook = clean(data.get("hook"))
    if word_count(hook) < 7:
        raise RuntimeError("Opening hook is too short.")

    seen = set()
    global_number = 0
    first_narration = ""

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict) or as_int(
            part.get("part"), f"part {p}"
        ) != p:
            raise RuntimeError(f"Invalid part numbering at {p}.")

        rows = part.get("scenes")
        if not isinstance(rows, list) or len(rows) != scenes_per_part:
            raise RuntimeError(
                f"Part {p} must have exactly {scenes_per_part} scenes."
            )

        part["part_id"] = f"part_{p:02d}"
        part["title"] = clean(part.get("title")) or f"भाग {p}"

        for s, scene in enumerate(rows, 1):
            global_number += 1
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid scene {p}:{s}.")

            scene_id = f"part_{p:02d}_scene_{s:02d}"
            scene["part"] = p
            scene["scene"] = s
            scene["scene_id"] = scene_id
            scene["global_scene_number"] = global_number

            for field in (
                "purpose", "narration", "visual", "dialogue", "suspense"
            ):
                value = scene.get(field, "")
                if not isinstance(value, str):
                    raise RuntimeError(f"{scene_id}: {field} must be text.")
                scene[field] = value.strip()

            if not scene["narration"] or not scene["visual"]:
                raise RuntimeError(
                    f"{scene_id}: narration or visual is empty."
                )
            if scene_id in seen:
                raise RuntimeError(f"Duplicate scene ID: {scene_id}.")
            seen.add(scene_id)

            if p == 1 and s == 1:
                first_narration = scene["narration"]

    if len(seen) != parts_count * scenes_per_part:
        raise RuntimeError("Scene mapping validation failed.")
    if not first_narration.startswith(hook):
        raise RuntimeError("First scene narration must start with the hook.")

    data["title"] = title
    data["topic"] = topic or title
    data["status"] = "completed"
    return data


def main():
    log("=" * 58)
    log("KATHA LOK AI — STORY BUILDER")
    log("External API calls: 0")
    log("=" * 58)

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    format_type = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    story_text = clean(get_story_text(config))
    parts_count = as_int(get_parts(config), "PARTS")
    scenes_per_part = as_int(get_scenes(config), "SCENES")

    if format_type not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")
    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive.")
    if not topic and not story_text:
        raise RuntimeError("Provide TOPIC or STORY_TEXT in Input/topic.txt.")

    title = derive_title(topic, story_text)
    if not title:
        raise RuntimeError("Could not determine story title.")

    plan = validate_plan(
        read_json(PLAN_FILE),
        format_type,
        topic,
        parts_count,
        scenes_per_part,
    )

    if format_type == "short" and story_text:
        total_words = word_count(story_text)
        low = parts_count * MIN_WORDS_PER_SHORT_PART
        high = parts_count * MAX_WORDS_PER_SHORT_PART
        log("SHORT STORY SOURCE WORD COUNT:", total_words)
        log("TARGET WORD COUNT:", f"{low}-{high} across {parts_count} part(s)")
        if total_words < low:
            log(
                "WARNING: source is shorter than the requested target. "
                "No invented padding or unsupported plot will be added."
            )
        elif total_words > high:
            log(
                "INFO: source is longer than the target. All source text "
                "will be preserved and distributed in order."
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
                log("Saved story checkpoint invalid; rebuilding:", exc)

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
    log("Words per part:", data["part_word_counts"])

    if format_type == "short":
        for number, count in enumerate(data["part_word_counts"], 1):
            status = (
                "IN RANGE"
                if MIN_WORDS_PER_SHORT_PART <= count <= MAX_WORDS_PER_SHORT_PART
                else "OUTSIDE TARGET"
            )
            log(f"Part {number}: {count} words — {status}")

    if not story_text:
        log(
            "WARNING: topic-only mode uses an offline template, not a "
            "fully AI-written story."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Story build cancelled.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
