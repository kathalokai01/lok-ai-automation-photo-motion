
#!/usr/bin/env python3
"""Katha Lok AI story builder: strict short-part checks and stable schema."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from input_config import (
    load_and_validate,
    get_format,
    get_topic,
    get_story_text,
    get_parts,
    get_scenes,
)

STORY_DIR = ROOT / "output" / "story"
PLAN_FILE = STORY_DIR / "story_plan.json"
OUTPUT_FILE = STORY_DIR / "story.json"
TITLE_FILE = STORY_DIR / "final_title.txt"
FINGERPRINT_FILE = STORY_DIR / "story.fingerprint"

VERSION = "story-builder-v7-strict-short-parts-schema-compatible"
SHORT_MIN_WORDS = 120
SHORT_MAX_WORDS = 150
SHORT_TARGET_WORDS = 135


def log(*items):
    print(*items, flush=True)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def words(text):
    return re.findall(r"\S+", clean(text))


def word_count(text):
    return len(words(text))


def digest(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
    """Split at sentence boundaries while retaining source order."""
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
    """Prefer an explicit topic; otherwise derive a title from story clues."""
    if clean_title(topic):
        return clean_title(topic)[:100]

    units = sentences(story_text)
    if not units:
        return ""

    markers = (
        "रहस्य", "सच", "गायब", "लापता", "अचानक", "खुलासा",
        "राज", "धोखा", "आखिरी", "आखिर", "क्यों", "कैसे",
        "मौत", "चिट्ठी", "सबूत", "दरवाजा", "आवाज", "संदेह",
        "secret", "mystery", "missing", "truth", "betrayal",
        "letter", "disappeared", "revealed",
    )

    def score(pair):
        index, sentence = pair
        lower = sentence.casefold()
        clue_score = sum(3 for marker in markers if marker in lower)
        length_score = min(len(sentence), 150) / 50
        # On a tie, prefer a later story clue over the first line.
        later_bonus = min(index, 8) * 0.15
        return clue_score + length_score + later_bonus

    selected = max(enumerate(units), key=score)[1]
    selected = re.sub(
        r"^(और|लेकिन|मगर|फिर|तभी|क्योंकि|इसलिए)\s+",
        "",
        selected,
        flags=re.IGNORECASE,
    )
    title_words = selected.split()
    if len(title_words) > 9:
        selected = " ".join(title_words[:9]).rstrip(" ,;:-") + "…"

    return clean_title(selected)[:90] or "Katha Lok AI कहानी"


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
            "Story plan missing or invalid. Run generate_story_plan.py first."
        )

    if clean(plan.get("format")).lower() != format_type:
        raise RuntimeError(
            "Story plan format differs from Input/topic.txt. "
            "Regenerate the story plan."
        )

    plan_topic = clean(plan.get("topic"))
    if topic and plan_topic and topic != plan_topic:
        raise RuntimeError(
            "Story plan topic differs from the current input. "
            "Regenerate the story plan."
        )

    expected_total = parts_count * scenes_per_part
    actual = (
        as_int(plan.get("parts_count"), "plan.parts_count"),
        as_int(plan.get("scenes_per_part"), "plan.scenes_per_part"),
        as_int(plan.get("total_scenes"), "plan.total_scenes"),
    )
    expected = (parts_count, scenes_per_part, expected_total)
    if actual != expected:
        raise RuntimeError(
            f"Story plan counts {actual} do not match {expected}. "
            "Regenerate the plan before continuing."
        )

    plan_parts = plan.get("parts")
    if not isinstance(plan_parts, list) or len(plan_parts) != parts_count:
        raise RuntimeError("Story plan has an invalid parts list.")

    global_number = 0
    for p, part in enumerate(plan_parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid plan part {p}.")
        if as_int(part.get("part"), f"plan part {p}") != p:
            raise RuntimeError(f"Incorrect plan part number: {p}.")

        scene_rows = part.get("scenes")
        if (
            not isinstance(scene_rows, list)
            or len(scene_rows) != scenes_per_part
        ):
            raise RuntimeError(f"Plan part {p} has the wrong scene count.")

        for s, row in enumerate(scene_rows, 1):
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


def split_balanced(text, count):
    """Split text into exactly count non-empty, ordered scene chunks."""
    units = sentences(text)
    if not units:
        raise RuntimeError("Story text contains no usable sentences.")
    if count < 1:
        raise RuntimeError("Split count must be positive.")

    if len(units) < count:
        source_words = words(" ".join(units))
        if len(source_words) < count:
            raise RuntimeError(
                f"Only {len(source_words)} words are available for {count} "
                "scenes. Add source text or reduce SCENES."
            )
        chunks = []
        for i in range(count):
            start = round(i * len(source_words) / count)
            end = round((i + 1) * len(source_words) / count)
            chunks.append(" ".join(source_words[start:end]).strip())
        return chunks

    chunks = []
    cursor = 0

    for group_index in range(count):
        groups_left = count - group_index
        units_left = len(units) - cursor

        if groups_left == 1:
            chunks.append(" ".join(units[cursor:]).strip())
            break

        target = sum(word_count(x) for x in units[cursor:]) / groups_left
        max_take = units_left - (groups_left - 1)
        group = []
        current_words = 0

        while cursor < len(units) and len(group) < max_take:
            candidate = units[cursor]
            candidate_count = word_count(candidate)

            if group and abs(current_words - target) <= abs(
                current_words + candidate_count - target
            ):
                break

            group.append(candidate)
            current_words += candidate_count
            cursor += 1

            if current_words >= target:
                break

        if not group and cursor < len(units):
            group.append(units[cursor])
            cursor += 1

        chunks.append(" ".join(group).strip())

    if len(chunks) != count or any(not chunk for chunk in chunks):
        raise RuntimeError("Could not split the story into the requested scenes.")

    return chunks


def split_short_parts(text, count):
    """
    Split into exactly count parts, each 120-150 words.
    Sentence order is preserved. No invented padding is added.
    """
    units = sentences(text)
    if not units:
        raise RuntimeError("Short story contains no usable sentences.")

    total = sum(word_count(unit) for unit in units)
    minimum_total = count * SHORT_MIN_WORDS
    maximum_total = count * SHORT_MAX_WORDS

    if total < minimum_total or total > maximum_total:
        raise RuntimeError(
            "SHORT WORD-COUNT CHECK FAILED: "
            f"source has {total} words for {count} part(s); "
            f"the allowed total is {minimum_total}-{maximum_total}. "
            "Provide a story within this range or revise PARTS. "
            "This offline script will not fabricate story text."
        )

    counts = [word_count(unit) for unit in units]
    prefix = [0]
    for amount in counts:
        prefix.append(prefix[-1] + amount)

    # Find sentence-boundary splits that keep every part in range.
    # Among valid splits, prefer parts close to 135 words.
    @lru_cache(maxsize=None)
    def solve(start, parts_left):
        if parts_left == 1:
            remaining = prefix[len(units)] - prefix[start]
            if SHORT_MIN_WORDS <= remaining <= SHORT_MAX_WORDS:
                return (abs(remaining - SHORT_TARGET_WORDS), [])
            return None

        best = None
        last_end = len(units) - (parts_left - 1)

        for end in range(start + 1, last_end + 1):
            amount = prefix[end] - prefix[start]

            if amount < SHORT_MIN_WORDS:
                continue
            if amount > SHORT_MAX_WORDS:
                break

            rest = solve(end, parts_left - 1)
            if rest is None:
                continue

            score = abs(amount - SHORT_TARGET_WORDS) + rest[0]
            candidate = (score, [end] + rest[1])

            if best is None or candidate[0] < best[0]:
                best = candidate

        return best

    solution = solve(0, count)
    if solution is None:
        actual = [word_count(unit) for unit in units]
        raise RuntimeError(
            "SHORT PART SPLIT FAILED: the source has enough total words, "
            "but sentence boundaries cannot produce exactly "
            f"{count} parts of {SHORT_MIN_WORDS}-{SHORT_MAX_WORDS} words. "
            "Adjust the story's sentence lengths or PARTS. "
            f"Sentence word counts: {actual}"
        )

    boundaries = solution[1] + [len(units)]
    chunks = []
    start = 0
    for end in boundaries:
        chunks.append(" ".join(units[start:end]).strip())
        start = end

    for index, chunk in enumerate(chunks, 1):
        amount = word_count(chunk)
        if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
            raise RuntimeError(
                f"Internal short-part validation failed for part {index}: "
                f"{amount} words."
            )

    return chunks


def topic_story_beats(topic, count):
    """
    Offline topic fallback. This is clearly marked as a template,
    not represented as AI-generated prose.
    """
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


def choose_hook(story_text, first_chunk):
    """Use the real opening rather than inserting unrelated boilerplate."""
    source_units = sentences(story_text)
    if source_units:
        # A hook that is already at the beginning avoids duplicate narration.
        return source_units[0]
    chunk_units = sentences(first_chunk)
    if chunk_units:
        return chunk_units[0]
    return clean(first_chunk)


def suspense_ending(text):
    """Return a story-grounded ending clue without inventing plot facts."""
    units = sentences(text)
    if not units:
        return ""

    ending = units[-1]
    if word_count(ending) > 24:
        ending = " ".join(words(ending)[-18:])

    return ending


def make_story(
    config,
    plan,
    format_type,
    title,
    topic,
    story_text,
    parts_count,
    scenes_per_part,
):
    part_hook = bool_value(config, "PART_HOOK", True)
    part_suspense = bool_value(config, "PART_SUSPENSE", True)
    final_resolution = bool_value(config, "FINAL_RESOLUTION", True)

    if story_text:
        if format_type == "short":
            part_texts = split_short_parts(story_text, parts_count)
            chunks_by_part = [
                split_balanced(part_text, scenes_per_part)
                for part_text in part_texts
            ]
            story_type = "user_supplied_story_short_word_validated"
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
            topic or title,
            parts_count * scenes_per_part,
        )
        chunks_by_part = [
            all_chunks[i * scenes_per_part:(i + 1) * scenes_per_part]
            for i in range(parts_count)
        ]
        story_type = "offline_topic_template_not_ai_generated"

    hook = choose_hook(story_text, chunks_by_part[0][0])
    if word_count(hook) < 1:
        raise RuntimeError("Could not create a usable opening hook.")

    parts = []
    global_number = 0
    part_word_counts = []

    for p, plan_part in enumerate(plan["parts"], 1):
        raw_chunks = chunks_by_part[p - 1]
        part_text = " ".join(raw_chunks).strip()
        part_word_counts.append(word_count(part_text))
        scene_rows = []

        for s, plan_scene in enumerate(plan_part["scenes"], 1):
            global_number += 1
            narration = clean(raw_chunks[s - 1])

            # The opening hook is sourced from the opening sentence, so it
            # should already occur at the beginning without duplicated text.
            if p == 1 and s == 1 and story_text:
                if not narration.startswith(hook):
                    raise RuntimeError(
                        "Opening hook is not at the start of scene 1. "
                        "Check the source sentence splitting."
                    )

            visual = clean(
                plan_scene.get("visual")
                or plan_scene.get("visual_prompt")
            )
            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene showing: "
                    + narration[:450]
                    + ". Natural expressions, realistic lighting, "
                    "consistent character identity and clothing, "
                    "natural exposure, realistic skin texture, "
                    "no cartoon style, no exaggerated contrast."
                )

            suspense = ""
            if s == scenes_per_part:
                if p < parts_count and part_suspense:
                    suspense = suspense_ending(part_text)
                elif p == parts_count and final_resolution:
                    suspense = (
                        "Final part: resolve the established story using "
                        "only supported events; do not invent a new reveal."
                    )

            scene_rows.append({
                "part": p,
                "scene": s,
                "scene_id": f"part_{p:02d}_scene_{s:02d}",
                "global_scene_number": global_number,
                "purpose": clean(
                    plan_scene.get("purpose")
                ) or "कहानी को आगे बढ़ाना",
                "narration": narration,
                "visual": visual,
                "dialogue": clean(plan_scene.get("dialogue")),
                "suspense": suspense,
            })

        parts.append({
            "part": p,
            "part_id": f"part_{p:02d}",
            "title": clean(plan_part.get("title")) or f"भाग {p}",
            "scenes": scene_rows,
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
            "minimum": SHORT_MIN_WORDS,
            "maximum": SHORT_MAX_WORDS,
            "target": SHORT_TARGET_WORDS,
            "validation": (
                "strict_for_user_supplied_story"
                if format_type == "short" and story_text
                else "not_guaranteed_for_offline_topic_template"
            ),
        } if format_type == "short" else None,
        "source_text_sha256": hashlib.sha256(
            clean(story_text).encode("utf-8")
        ).hexdigest(),
        "generation_method": "offline_no_api",
        "mapping_version": 5,
        "_generation": {
            "script_version": VERSION,
            "format": format_type,
            "parts": parts_count,
            "scenes_per_part": scenes_per_part,
            "total_scenes": parts_count * scenes_per_part,
            "mapping_version": 5,
            "opening_hook_required": True,
            "part_hook_required": part_hook,
            "part_suspense_required": part_suspense,
            "final_resolution_requested": final_resolution,
            "scene_order": "part_then_scene",
            "api_calls": 0,
        },
    }


def validate_story(
    data,
    format_type,
    title,
    topic,
    parts_count,
    scenes_per_part,
    has_source_story,
):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Story root/status is invalid.")
    if clean(data.get("format")).lower() != format_type:
        raise RuntimeError("Story format mismatch.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(f"Expected exactly {parts_count} parts.")

    hook = clean(data.get("hook"))
    if not hook:
        raise RuntimeError("Opening hook is empty.")

    seen = set()
    global_number = 0
    first_narration = ""

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid part record at {p}.")
        if as_int(part.get("part"), f"part {p}") != p:
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
                "purpose",
                "narration",
                "visual",
                "dialogue",
                "suspense",
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

    if has_source_story and not first_narration.startswith(hook):
        raise RuntimeError("First scene narration must start with the hook.")

    if format_type == "short" and has_source_story:
        counts = [
            word_count(" ".join(scene["narration"] for scene in part["scenes"]))
            for part in parts
        ]
        for index, amount in enumerate(counts, 1):
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"Part {index} has {amount} words; expected "
                    f"{SHORT_MIN_WORDS}-{SHORT_MAX_WORDS}."
                )
        data["part_word_counts"] = counts

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

    settings = {
        key: config.get(key)
        for key in (
            "AUDIENCE",
            "STORY_LENGTH",
            "SCENE_DURATION",
            "PART_HOOK",
            "PART_SUSPENSE",
            "FINAL_RESOLUTION",
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
                    saved,
                    format_type,
                    title,
                    topic,
                    parts_count,
                    scenes_per_part,
                    bool(story_text),
                )
                log("RESUME: matching validated story reused.")
                return
            except (RuntimeError, TypeError, ValueError) as exc:
                log("Saved story checkpoint invalid; rebuilding:", exc)

    data = make_story(
        config,
        plan,
        format_type,
        title,
        topic,
        story_text,
        parts_count,
        scenes_per_part,
    )
    data = validate_story(
        data,
        format_type,
        title,
        topic,
        parts_count,
        scenes_per_part,
        bool(story_text),
    )

    write_json(OUTPUT_FILE, data)
    atomic_write(TITLE_FILE, title + "\n")
    atomic_write(FINGERPRINT_FILE, fingerprint + "\n")

    log("STORY BUILD: SUCCESS")
    log("Output:", OUTPUT_FILE.relative_to(ROOT))
    log("Story type:", data["story_type"])
    log("Words per part:", data["part_word_counts"])

    if format_type == "short" and story_text:
        log("Short word-count validation: PASS")
    elif format_type == "short":
        log(
            "WARNING: Topic-only offline template is not guaranteed to "
            "reach 120-150 words per part."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"STORY BUILD ERROR: {exc}")
        sys.exit(1)
