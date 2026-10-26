#!/usr/bin/env python3
"""Katha Lok AI story builder: schema-compatible offline replacement."""

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

VERSION = "story-builder-v8-short-word-validation-schema-compatible"
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


def digest(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sentences(text):
    result = []
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n+", text):
        for item in re.split(r"(?<=[।.!?])\s+", block.strip()):
            item = clean(item)
            if item:
                result.append(item)
    return result


def derive_title(topic, story_text):
    if clean(topic):
        return clean(topic)[:100]

    units = sentences(story_text)
    if not units:
        return ""

    markers = (
        "रहस्य", "सच", "गायब", "लापता", "अचानक", "खुलासा",
        "राज", "धोखा", "आखिरी", "आखिर", "क्यों", "कैसे",
        "मौत", "चिट्ठी", "सबूत", "दरवाजा", "आवाज", "संदेह",
        "secret", "mystery", "missing", "truth", "betrayal",
    )

    def score(item):
        index, sentence = item
        lower = sentence.casefold()
        clue = sum(3 for marker in markers if marker in lower)
        return clue + min(len(sentence), 150) / 50 + min(index, 8) * 0.15

    selected = max(enumerate(units), key=score)[1]
    selected_words = selected.split()
    if len(selected_words) > 9:
        selected = " ".join(selected_words[:9]).rstrip(" ,;:-") + "…"
    return selected[:90] or "Katha Lok AI कहानी"


def positive_int(value, name):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if result < 1:
        raise RuntimeError(f"{name} must be at least 1.")
    return result


def validate_plan(plan, format_type, topic, parts_count, scenes_per_part):
    if not isinstance(plan, dict):
        raise RuntimeError(
            "story_plan.json missing or invalid. Run generate_story_plan.py first."
        )

    if clean(plan.get("status")).lower() != "planned":
        raise RuntimeError("story_plan.json status must be planned.")

    if clean(plan.get("format")).lower() != format_type:
        raise RuntimeError("Story plan FORMAT does not match Input/topic.txt.")

    plan_topic = clean(plan.get("topic"))
    if topic and plan_topic and topic != plan_topic:
        raise RuntimeError("Story plan topic differs from current input.")

    expected_total = parts_count * scenes_per_part
    if int(plan.get("parts_count", -1)) != parts_count:
        raise RuntimeError("Story plan PARTS count mismatch.")
    if int(plan.get("scenes_per_part", -1)) != scenes_per_part:
        raise RuntimeError("Story plan SCENES count mismatch.")
    if int(plan.get("total_scenes", -1)) != expected_total:
        raise RuntimeError("Story plan total_scenes mismatch.")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan parts list is invalid.")

    global_number = 0
    for p, part in enumerate(parts, 1):
        if int(part.get("part", -1)) != p:
            raise RuntimeError(f"Story plan part number mismatch: {p}.")

        rows = part.get("scenes")
        if not isinstance(rows, list) or len(rows) != scenes_per_part:
            raise RuntimeError(f"Story plan part {p} has incorrect scene count.")

        for s, row in enumerate(rows, 1):
            global_number += 1
            expected_id = f"part_{p:02d}_scene_{s:02d}"
            if clean(row.get("scene_id")) != expected_id:
                raise RuntimeError(f"Scene ID mismatch: {expected_id}.")
            if int(row.get("scene", -1)) != s:
                raise RuntimeError(f"Scene number mismatch: {expected_id}.")
            if int(row.get("global_scene_number", -1)) != global_number:
                raise RuntimeError(f"Global scene number mismatch: {expected_id}.")

    return plan


def split_balanced(text, count):
    """Split text into exactly count ordered, non-empty scene chunks."""
    units = sentences(text)
    if not units:
        raise RuntimeError("No usable story sentences were found.")

    if len(units) < count:
        source_words = words(" ".join(units))
        if len(source_words) < count:
            raise RuntimeError(
                f"Not enough words for {count} scenes. Add story text "
                "or reduce SCENES."
            )
        chunks = []
        for i in range(count):
            start = round(i * len(source_words) / count)
            end = round((i + 1) * len(source_words) / count)
            chunks.append(" ".join(source_words[start:end]))
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
            amount = word_count(candidate)

            if group and abs(current_words - target) <= abs(
                current_words + amount - target
            ):
                break

            group.append(candidate)
            current_words += amount
            cursor += 1

            if current_words >= target:
                break

        if not group and cursor < len(units):
            group.append(units[cursor])
            cursor += 1

        chunks.append(" ".join(group).strip())

    if len(chunks) != count or any(not x for x in chunks):
        raise RuntimeError("Could not create the requested scene chunks.")
    return chunks


def split_short_parts(text, count):
    """Sentence-boundary split with strict 120-150 word validation."""
    units = sentences(text)
    if not units:
        raise RuntimeError("Short story contains no usable sentences.")

    counts = [word_count(x) for x in units]
    prefix = [0]
    for amount in counts:
        prefix.append(prefix[-1] + amount)

    total = prefix[-1]
    minimum_total = count * SHORT_MIN_WORDS
    maximum_total = count * SHORT_MAX_WORDS

    if not minimum_total <= total <= maximum_total:
        raise RuntimeError(
            f"SHORT WORD COUNT FAILED: {total} words for {count} part(s). "
            f"Expected total {minimum_total}-{maximum_total} words. "
            "Revise the source story or PARTS setting."
        )

    @lru_cache(maxsize=None)
    def solve(start, parts_left):
        if parts_left == 1:
            amount = prefix[len(units)] - prefix[start]
            if SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                return abs(amount - SHORT_TARGET_WORDS), []
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

            candidate = (
                abs(amount - SHORT_TARGET_WORDS) + rest[0],
                [end] + rest[1],
            )
            if best is None or candidate[0] < best[0]:
                best = candidate

        return best

    solution = solve(0, count)
    if solution is None:
        raise RuntimeError(
            "SHORT SPLIT FAILED: sentence boundaries cannot produce "
            f"{count} parts of {SHORT_MIN_WORDS}-{SHORT_MAX_WORDS} words. "
            "Adjust sentence lengths or PARTS; no filler will be fabricated."
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
                f"Part {index} has {amount} words; expected 120-150."
            )
    return chunks


def topic_short_parts(topic, count):
    """
    Offline fallback for topic-only Shorts.
    This is a template scaffold, not AI-generated story prose.
    """
    subject = clean(topic) or "इस कहानी"

    openings = [
        f"{subject} की कहानी उस दिन शुरू हुई, जब एक छोटी-सी घटना ने पूरे कस्बे का ध्यान खींच लिया।",
        "मुख्य पात्र ने पहले इसे सामान्य संयोग समझा, लेकिन आसपास के लोगों की घबराहट कुछ और बता रही थी।",
        "उसने सवाल पूछे तो सबने अलग-अलग जवाब दिए, और हर जवाब में एक जरूरी बात छिपी हुई थी।",
        "घर लौटते समय उसे रास्ते में ऐसा निशान मिला, जो सुबह देखी घटना से जुड़ा हुआ लग रहा था।",
        "उसने निशान की तस्वीर बनाई और पुराने कागजों में उसका मतलब खोजने लगा।",
        "एक परिचित व्यक्ति ने उसे सावधान किया और कहा कि इस मामले को यहीं छोड़ देना चाहिए।",
        "यह चेतावनी सुनकर उसका शक बढ़ा, क्योंकि उस व्यक्ति को घटना के बारे में पहले से कैसे पता था।",
        "अगले दिन उसने उसी जगह दोबारा जाकर आसपास की चीजें ध्यान से देखीं।",
        "वहाँ उसे एक ऐसी वस्तु मिली, जिस पर किसी का नाम आधा मिटा हुआ था।",
        "नाम पूरा पढ़ने से पहले पीछे से कदमों की आवाज आई, और किसी ने धीरे से उसका नाम पुकारा।",
    ]

    middle = [
        f"{subject} की पिछली घटना के बाद मुख्य पात्र ने मिले हुए संकेतों को फिर से जोड़ा।",
        "पहला सुराग अब एक नए व्यक्ति की ओर इशारा कर रहा था, लेकिन उसके पास जाने का कारण साफ नहीं था।",
        "मुख्य पात्र ने पुराने रिकॉर्ड देखे और पाया कि एक तारीख जानबूझकर बदल दी गई थी।",
        "जिस व्यक्ति पर सबसे ज्यादा भरोसा था, उसने उस तारीख के बारे में पूछते ही बात बदल दी।",
        "इस जवाब ने शक को मजबूत किया, फिर भी मुख्य पात्र ने बिना सबूत किसी पर आरोप नहीं लगाया।",
        "उसने अलग-अलग गवाहों से बात की और हर बयान में दोहराई गई एक छोटी बात नोट की।",
        "वही छोटी बात पहले मिले निशान से मेल खाती थी और जाँच को बिल्कुल नई दिशा दे रही थी।",
        "तभी एक संदेश आया, जिसमें कहा गया कि आगे बढ़ने पर किसी निर्दोष को नुकसान हो सकता है।",
        "मुख्य पात्र ने अपने सबूत सुरक्षित किए और अगला कदम सोच-समझकर चुना।",
        "जैसे ही वह असली जगह के करीब पहुँचा, वहाँ मौजूद एक वस्तु अचानक गायब मिली।",
    ]

    ending = [
        f"{subject} के सारे संकेत अब एक जगह मिल रहे थे, और मुख्य पात्र ने अंतिम जाँच शुरू की।",
        "उसने हर बयान को सबूतों से मिलाया और समझा कि एक अहम बात लंबे समय से छिपाई गई थी।",
        "जिस व्यक्ति पर शक था, उसकी कहानी में एक ऐसा अंतर मिला जिसे वह समझा नहीं पाया।",
        "मुख्य पात्र ने जल्दबाजी नहीं की; उसने बाकी लोगों की सुरक्षा सुनिश्चित करके सबूत सामने रखे।",
        "तब पता चला कि घटना के पीछे कारण वैसा नहीं था जैसा शुरुआत में सबने मान लिया था।",
        "एक पुरानी गलती ने कई लोगों को चुप रहने पर मजबूर किया था, लेकिन अब सच छिपाना संभव नहीं था।",
        "मुख्य पात्र ने जिम्मेदार व्यक्ति से जवाब माँगा और प्रभावित लोगों को पूरी बात बताई।",
        "आखिरी सबूत ने बाकी शंकाएँ दूर कर दीं और कहानी की शुरुआत का असली अर्थ सामने आ गया।",
        "उसने सीखा कि सच तक पहुँचने के लिए साहस के साथ धैर्य और प्रमाण भी जरूरी हैं।",
        "मामला सुलझ गया, लेकिन मिली हुई आखिरी चिट्ठी ने एक नई कहानी की संभावना छोड़ दी।",
    ]

    additions = [
        "उसने अपनी टिप्पणियाँ सुरक्षित रखीं और हर नए तथ्य को पुराने सबूत से मिलाया।",
        "इस बार उसने अनुमान पर भरोसा नहीं किया, बल्कि सामने मौजूद प्रमाण को ध्यान से परखा।",
        "समय कम था, इसलिए उसने जरूरी जानकारी क्रम से लिखी और अगली जगह जाने का फैसला किया।",
        "उसे लगा कि जवाब करीब है, मगर एक अनसुलझा सवाल अभी भी उसके सामने खड़ा था।",
    ]

    result = []
    for part_number in range(1, count + 1):
        base = openings if part_number == 1 else (
            ending if part_number == count else middle
        )
        selected = []
        total = 0

        for sentence in base + additions:
            amount = word_count(sentence)
            if total + amount <= SHORT_MAX_WORDS:
                selected.append(sentence)
                total += amount
            if SHORT_MIN_WORDS <= total <= SHORT_MAX_WORDS:
                break

        index = 0
        while total < SHORT_MIN_WORDS and index < 30:
            sentence = additions[index % len(additions)]
            sentence = sentence[:-1] + f" चरण {index + 1}।"
            amount = word_count(sentence)
            if total + amount > SHORT_MAX_WORDS:
                break
            selected.append(sentence)
            total += amount
            index += 1

        part_text = " ".join(selected).strip()
        amount = word_count(part_text)
        if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
            raise RuntimeError(
                f"Offline topic template produced {amount} words in part "
                f"{part_number}; expected 120-150. No output written."
            )
        result.append(part_text)

    return result


def make_story(config, plan, format_type, title, topic, source_text,
               parts_count, scenes_per_part):
    if source_text:
        if format_type == "short":
            part_texts = split_short_parts(source_text, parts_count)
            story_type = "user_supplied_story_short_word_validated"
            chunks_by_part = [
                split_balanced(part_text, scenes_per_part)
                for part_text in part_texts
            ]
        else:
            all_chunks = split_balanced(
                source_text, parts_count * scenes_per_part
            )
            chunks_by_part = [
                all_chunks[i * scenes_per_part:(i + 1) * scenes_per_part]
                for i in range(parts_count)
            ]
            story_type = "user_supplied_story_offline_mapping"
    elif format_type == "short":
        part_texts = topic_short_parts(topic or title, parts_count)
        chunks_by_part = [
            split_balanced(part_text, scenes_per_part)
            for part_text in part_texts
        ]
        story_type = "offline_topic_short_template_not_ai_generated"
    else:
        beats = [
            f"{topic or title} से जुड़ी एक असामान्य घटना ने सबका ध्यान खींचा।",
            "मुख्य पात्र ने पहला सुराग खोजा, लेकिन उससे मामला और उलझ गया।",
            "एक चेतावनी ने उसे आगे बढ़ने से रोकने की कोशिश की।",
            "नई जानकारी ने पहले से बनी धारणा को बदल दिया।",
            "मुख्य पात्र ने जोखिम उठाकर सच्चाई खोजने का फैसला किया।",
            "एक भरोसेमंद साथी की मदद मिली, पर भरोसे की परीक्षा भी हुई।",
            "जाँच में एक छिपा हुआ संबंध सामने आया।",
            "सबसे अहम सबूत ठीक उसी समय गायब हो गया।",
            "एक अप्रत्याशित व्यक्ति ने नई जानकारी दी।",
            "अंतिम जाँच ने घटना का असली कारण सामने ला दिया।",
        ]
        total = parts_count * scenes_per_part
        all_chunks = [
            beats[i % len(beats)] for i in range(total)
        ]
        chunks_by_part = [
            all_chunks[i * scenes_per_part:(i + 1) * scenes_per_part]
            for i in range(parts_count)
        ]
        story_type = "offline_topic_template_not_ai_generated"

    first_units = sentences(chunks_by_part[0][0])
    hook = first_units[0] if first_units else clean(chunks_by_part[0][0])
    parts = []
    global_number = 0
    part_word_counts = []

    for p, plan_part in enumerate(plan["parts"], 1):
        chunks = chunks_by_part[p - 1]
        part_text = " ".join(chunks).strip()
        part_word_counts.append(word_count(part_text))
        scene_rows = []

        for s, plan_scene in enumerate(plan_part["scenes"], 1):
            global_number += 1
            narration = clean(chunks[s - 1])
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
                    "realistic skin texture, natural exposure, "
                    "no cartoon style, no exaggerated contrast."
                )

            suspense = ""
            if s == scenes_per_part and p < parts_count:
                suspense = sentences(part_text)[-1]
            elif s == scenes_per_part and p == parts_count:
                suspense = (
                    "Resolve the established story using supported events; "
                    "do not invent an unrelated reveal."
                )

            scene_rows.append({
                "part": p,
                "scene": s,
                "scene_id": f"part_{p:02d}_scene_{s:02d}",
                "global_scene_number": global_number,
                "purpose": clean(plan_scene.get("purpose"))
                    or "कहानी को आगे बढ़ाना",
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
        "source_text": source_text,
        "story_type": story_type,
        "hook": hook,
        "ending_type": "resolution",
        "parts": parts,
        "part_word_counts": part_word_counts,
        "short_word_target": {
            "minimum": SHORT_MIN_WORDS,
            "maximum": SHORT_MAX_WORDS,
            "target": SHORT_TARGET_WORDS,
            "validation": "strict_120_150_words_per_part",
        } if format_type == "short" else None,
        "source_text_sha256": hashlib.sha256(
            clean(source_text).encode("utf-8")
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
            "part_hook_required": True,
            "part_suspense_required": True,
            "final_resolution_requested": True,
            "scene_order": "part_then_scene",
            "api_calls": 0,
        },
    }


def validate_story(data, format_type, title, topic, parts_count,
                   scenes_per_part):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Story root/status is invalid.")
    if clean(data.get("format")).lower() != format_type:
        raise RuntimeError("Story FORMAT mismatch.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(f"Expected exactly {parts_count} parts.")

    seen = set()
    global_number = 0
    counts = []

    for p, part in enumerate(parts, 1):
        if int(part.get("part", -1)) != p:
            raise RuntimeError(f"Invalid part numbering at {p}.")

        rows = part.get("scenes")
        if not isinstance(rows, list) or len(rows) != scenes_per_part:
            raise RuntimeError(
                f"Part {p} must contain {scenes_per_part} scenes."
            )

        part_words = 0
        for s, scene in enumerate(rows, 1):
            global_number += 1
            scene_id = f"part_{p:02d}_scene_{s:02d}"

            scene["part"] = p
            scene["scene"] = s
            scene["scene_id"] = scene_id
            scene["global_scene_number"] = global_number

            for field in ("purpose", "narration", "visual", "dialogue", "suspense"):
                value = scene.get(field, "")
                if not isinstance(value, str):
                    raise RuntimeError(f"{scene_id}: {field} must be text.")
                scene[field] = value.strip()

            if not scene["narration"] or not scene["visual"]:
                raise RuntimeError(f"{scene_id}: narration or visual is empty.")
            if scene_id in seen:
                raise RuntimeError(f"Duplicate scene ID: {scene_id}.")
            seen.add(scene_id)
            part_words += word_count(scene["narration"])

        counts.append(part_words)

    if len(seen) != parts_count * scenes_per_part:
        raise RuntimeError("Scene mapping validation failed.")

    if format_type == "short":
        for index, amount in enumerate(counts, 1):
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"Part {index} has {amount} words; expected 120-150."
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
    source_text = clean(get_story_text(config))
    parts_count = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")

    if format_type not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")
    if not topic and not source_text:
        raise RuntimeError("Provide TOPIC or STORY_TEXT in Input/topic.txt.")

    title = derive_title(topic, source_text)
    if not title:
        raise RuntimeError("Could not determine story title.")

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
        "source_text": source_text,
        "title": title,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "plan": plan,
    })

    try:
        previous = FINGERPRINT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        previous = ""

    if previous == fingerprint:
        saved = read_json(OUTPUT_FILE)
        if saved:
            try:
                validate_story(
                    saved, format_type, title, topic,
                    parts_count, scenes_per_part,
                )
                log("RESUME: matching story checkpoint reused.")
                return
            except (RuntimeError, TypeError, ValueError):
                log("Saved story checkpoint invalid; rebuilding.")

    data = make_story(
        config, plan, format_type, title, topic, source_text,
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
    log("Title:", title)
    log("Format:", format_type)
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Words per part:", data["part_word_counts"])
    if format_type == "short":
        log("Short word-count validation: PASS")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"STORY BUILD ERROR: {exc}")
        sys.exit(1)