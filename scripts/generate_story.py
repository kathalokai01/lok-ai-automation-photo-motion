
#!/usr/bin/env python3
"""Katha Lok AI story builder: schema-safe story structure and validation."""

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

VERSION = "story-builder-v9-schema-safe-structured"
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
        data, ensure_ascii=False, sort_keys=True, default=str
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
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def sentences(text):
    result = []
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n+", text):
        for item in re.split(r"(?<=[।.!?！？])\s+", block.strip()):
            item = clean(item)
            if item:
                result.append(item)
    return result


def positive_int(value, name):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if result < 1:
        raise RuntimeError(f"{name} must be at least 1.")
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


def validate_plan(plan, mode, topic, parts_count, scenes_per_part):
    if not isinstance(plan, dict):
        raise RuntimeError(
            "story_plan.json missing or invalid. Run generate_story_plan.py first."
        )
    if clean(plan.get("status")).lower() != "planned":
        raise RuntimeError("story_plan.json status must be planned.")
    if clean(plan.get("format")).lower() != mode:
        raise RuntimeError("Story plan FORMAT does not match Input/topic.txt.")

    plan_topic = clean(plan.get("topic"))
    if topic and plan_topic and topic != plan_topic:
        raise RuntimeError("Story plan topic differs from current input.")

    total = parts_count * scenes_per_part
    expected = {
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total,
    }
    for key, value in expected.items():
        if int(plan.get(key, -1)) != value:
            raise RuntimeError(f"Story plan {key} mismatch.")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan parts list is invalid.")

    global_no = 0
    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != p:
            raise RuntimeError(f"Story plan part number mismatch: {p}.")
        rows = part.get("scenes")
        if not isinstance(rows, list) or len(rows) != scenes_per_part:
            raise RuntimeError(f"Story plan part {p} has incorrect scene count.")

        for s, row in enumerate(rows, 1):
            global_no += 1
            scene_id = f"part_{p:02d}_scene_{s:02d}"
            if not isinstance(row, dict):
                raise RuntimeError(f"Invalid planned scene: {scene_id}")
            if clean(row.get("scene_id")) != scene_id:
                raise RuntimeError(f"Scene ID mismatch: {scene_id}")
            if int(row.get("scene", -1)) != s:
                raise RuntimeError(f"Scene number mismatch: {scene_id}")
            if int(row.get("global_scene_number", -1)) != global_no:
                raise RuntimeError(f"Global scene number mismatch: {scene_id}")
            if not clean(row.get("purpose")):
                raise RuntimeError(f"Missing scene purpose: {scene_id}")
    return plan


def split_balanced(text, count):
    """Split text into count ordered, non-empty chunks."""
    units = sentences(text)
    if not units:
        raise RuntimeError("No usable story sentences were found.")

    if len(units) < count:
        source_words = words(" ".join(units))
        if len(source_words) < count:
            raise RuntimeError(
                f"Not enough words for {count} scenes. Add story text or reduce SCENES."
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
        if groups_left == 1:
            chunks.append(" ".join(units[cursor:]).strip())
            break

        units_left = len(units) - cursor
        max_take = units_left - (groups_left - 1)
        target = sum(word_count(x) for x in units[cursor:]) / groups_left
        group = []
        amount = 0

        while cursor < len(units) and len(group) < max_take:
            candidate = units[cursor]
            n = word_count(candidate)
            if group and abs(amount - target) <= abs(amount + n - target):
                break
            group.append(candidate)
            amount += n
            cursor += 1
            if amount >= target:
                break

        if not group and cursor < len(units):
            group.append(units[cursor])
            cursor += 1
        chunks.append(" ".join(group).strip())

    if len(chunks) != count or any(not item for item in chunks):
        raise RuntimeError("Could not create the requested scene chunks.")
    return chunks


def split_short_parts(text, count):
    """Split only at sentence boundaries; enforce 120-150 words per part."""
    units = sentences(text)
    if not units:
        raise RuntimeError("Short story contains no usable sentences.")

    counts = [word_count(x) for x in units]
    prefix = [0]
    for amount in counts:
        prefix.append(prefix[-1] + amount)

    total = prefix[-1]
    if not count * SHORT_MIN_WORDS <= total <= count * SHORT_MAX_WORDS:
        raise RuntimeError(
            f"SHORT WORD COUNT FAILED: {total} words for {count} part(s). "
            f"Required total: {count * SHORT_MIN_WORDS}-"
            f"{count * SHORT_MAX_WORDS}. Revise story text or PARTS."
        )

    @lru_cache(maxsize=None)
    def solve(start, parts_left):
        if parts_left == 1:
            amount = prefix[len(units)] - prefix[start]
            if SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                return abs(amount - SHORT_TARGET_WORDS), ()
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
                (end,) + rest[1],
            )
            if best is None or candidate[0] < best[0]:
                best = candidate
        return best

    solution = solve(0, count)
    if solution is None:
        raise RuntimeError(
            "SHORT SPLIT FAILED: sentence boundaries cannot produce "
            f"{count} parts of 120-150 words. Adjust sentence lengths or PARTS. "
            "No filler text will be fabricated."
        )

    boundaries = list(solution[1]) + [len(units)]
    chunks, start = [], 0
    for end in boundaries:
        chunks.append(" ".join(units[start:end]).strip())
        start = end

    for index, chunk in enumerate(chunks, 1):
        amount = word_count(chunk)
        if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
            raise RuntimeError(f"Part {index} has {amount} words; expected 120-150.")
    return chunks


def topic_short_parts(topic, count):
    """
    Offline fallback only. It is not an AI story generator.
    Keeps a clear opening, progressive investigation and ending.
    """
    subject = clean(topic) or "इस कहानी"

    opening = [
        f"{subject} का रहस्य तब शुरू हुआ, जब एक अनजान घटना ने सबको चौंका दिया।",
        "पहले किसी ने उस घटना को गंभीरता से नहीं लिया, लेकिन एक छोटी बात मेल नहीं खा रही थी।",
        "मुख्य पात्र ने उस बात की जाँच की और पाया कि लोगों के बयान एक-दूसरे से अलग थे।",
        "उसने हर बयान लिख लिया, ताकि कोई जरूरी संकेत छूट न जाए।",
        "पुराने रिकॉर्ड में उसे ऐसा निशान मिला, जो घटना वाली जगह से जुड़ा था।",
        "निशान के पास मिली वस्तु पर आधा मिटा हुआ नाम लिखा था।",
        "नाम पढ़ने से पहले पीछे से कदमों की आवाज आई और कोई उसे पुकारने लगा।",
        "वह मुड़ा, लेकिन वहाँ कोई नहीं था; केवल एक बंद दरवाजा खुला पड़ा था।",
        "दरवाजे के भीतर उसे वही संकेत दिखाई दिया, जिसे वह रिकॉर्ड में देख चुका था।",
        "अब सवाल यह था कि कोई उससे पहले वहाँ कैसे पहुँच गया।",
    ]

    middle = [
        f"{subject} की जाँच में मुख्य पात्र ने नए संकेतों को पुराने सबूतों से मिलाया।",
        "एक गवाह ने अपना बयान बदल दिया और दावा किया कि उसे कुछ याद नहीं।",
        "उसके पुराने बयान में एक तारीख थी, जो रिकॉर्ड से मेल नहीं खाती थी।",
        "मुख्य पात्र ने अनुमान लगाने के बजाय मूल दस्तावेज़ की जाँच की।",
        "दस्तावेज़ में एक नाम छिपाया गया था, लेकिन उसकी लिखावट बाकी हिस्सों से अलग थी।",
        "वह नाम पहले मिले संकेत से जुड़ता था, इसलिए जाँच की दिशा बदल गई।",
        "तभी एक संदेश आया, जिसमें उसे आगे बढ़ने से रोकने की चेतावनी दी गई।",
        "उसने संदेश सुरक्षित रखा और यह पता लगाने लगा कि भेजने वाले को क्या मालूम था।",
        "जैसे ही वह अगले सुराग तक पहुँचा, जरूरी वस्तु अपनी जगह से गायब मिली।",
        "अब उसके पास समय कम था और भरोसा करने के लिए कोई साफ वजह नहीं थी।",
    ]

    finale = [
        f"{subject} के बिखरे संकेत अब एक ही जगह मिल रहे थे, इसलिए अंतिम जाँच शुरू हुई।",
        "मुख्य पात्र ने हर बयान को सबूतों से मिलाया और एक पुरानी गलती खोज निकाली।",
        "जिस बात को सबने सच मान लिया था, उसका कोई ठोस प्रमाण मौजूद नहीं था।",
        "उसने संबंधित लोगों को बुलाया और दस्तावेज़ क्रम से सामने रखे।",
        "एक जवाब ने पुराने बयान का विरोध किया और छिपी हुई बात उजागर होने लगी।",
        "मुख्य पात्र ने जल्दबाजी नहीं की; उसने बाकी प्रमाण भी जाँच लिए।",
        "तब समझ आया कि घटना का कारण शुरुआती शक से अलग था।",
        "आखिरी सबूत ने मुख्य प्रश्न का उत्तर दिया और जिम्मेदारी स्पष्ट कर दी।",
        "प्रभावित लोगों को सच बताया गया और गलत आरोप हटाए गए।",
        "मामला सुलझ गया, लेकिन अंतिम दस्तावेज़ में एक ऐसा संकेत था जिसकी जाँच बाकी थी।",
    ]

    additions = [
        "उसने हर नए तथ्य को पहले से मौजूद प्रमाण से मिलाया।",
        "इस बार उसने अफवाह पर भरोसा नहीं किया और मूल जानकारी की जाँच की।",
        "समय कम था, इसलिए उसने जरूरी विवरण सुरक्षित करके अगला कदम चुना।",
        "एक सवाल अब भी अनसुलझा था और उसका उत्तर आगे की जाँच पर निर्भर था।",
    ]

    result = []
    for part_no in range(1, count + 1):
        base = opening if part_no == 1 else (
            finale if part_no == count else middle
        )
        selected = []
        amount = 0
        pool = base + additions

        # Reuse a limited set of sentences only when required to meet the
        # configured word range. Topic-only mode remains explicitly labelled.
        for sentence in pool:
            n = word_count(sentence)
            if amount + n <= SHORT_MAX_WORDS:
                selected.append(sentence)
                amount += n
            if amount >= SHORT_MIN_WORDS:
                break

        add_index = 0
        while amount < SHORT_MIN_WORDS and add_index < 40:
            sentence = additions[add_index % len(additions)]
            sentence = sentence[:-1] + f" चरण {add_index + 1}।"
            n = word_count(sentence)
            if amount + n > SHORT_MAX_WORDS:
                break
            selected.append(sentence)
            amount += n
            add_index += 1

        text = " ".join(selected).strip()
        amount = word_count(text)
        if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
            raise RuntimeError(
                f"Offline topic template produced {amount} words for part "
                f"{part_no}; required 120-150."
            )
        result.append(text)
    return result


def topic_full_chunks(topic, total):
    """Offline structured outline; does not claim to be AI-generated prose."""
    subject = clean(topic) or "कहानी"
    beats = [
        f"{subject} से जुड़ी एक असामान्य घटना ने मुख्य पात्र की जिंदगी बदल दी।",
        "शुरुआती जाँच में एक ऐसा संकेत मिला जो सामान्य व्याख्या से मेल नहीं खाता था।",
        "मुख्य पात्र ने संबंधित लोगों से बात की, लेकिन उनके बयानों में अंतर था।",
        "एक पुराने दस्तावेज़ ने घटना की समयरेखा पर नया सवाल खड़ा कर दिया।",
        "एक करीबी व्यक्ति ने चेतावनी दी कि आगे बढ़ने से किसी को नुकसान हो सकता है।",
        "मुख्य पात्र ने उपलब्ध प्रमाण सुरक्षित किए और जल्दबाजी में आरोप नहीं लगाया।",
        "नया सुराग पहले निष्कर्ष को कमजोर करता था और जाँच को दूसरी दिशा में ले गया।",
        "एक अहम वस्तु गायब मिली, जबकि उसकी जगह पर ताजा निशान मौजूद थे।",
        "मुख्य पात्र ने गवाहों के बयान और दस्तावेज़ों को साथ रखकर तुलना की।",
        "एक विरोधाभास ने उस जानकारी पर सवाल उठाया जिसे सब सच मान रहे थे।",
        "मुख्य पात्र ने विरोधाभास की जड़ खोजने के लिए अंतिम जाँच की।",
        "सबूत सामने आने पर संबंधित व्यक्ति को अपने बयान की व्याख्या करनी पड़ी।",
        "घटना का कारण स्पष्ट हुआ और पहले लगाए गए कुछ अनुमान गलत निकले।",
        "मुख्य पात्र ने प्रमाणों के आधार पर जिम्मेदारी तय की और प्रभावित लोगों की मदद की।",
        "अंत में मुख्य सवाल का जवाब मिला, जबकि केवल समर्थित संकेत ही आगे के लिए छोड़े गए।",
    ]
    return [beats[i % len(beats)] for i in range(total)]


def make_story(plan, mode, title, topic, source_text, parts_count, scenes_per_part):
    if source_text:
        if mode == "short":
            part_texts = split_short_parts(source_text, parts_count)
            chunks_by_part = [
                split_balanced(part_text, scenes_per_part) for part_text in part_texts
            ]
            story_type = "user_supplied_story_short_word_validated"
        else:
            all_chunks = split_balanced(
                source_text, parts_count * scenes_per_part
            )
            chunks_by_part = [
                all_chunks[i * scenes_per_part:(i + 1) * scenes_per_part]
                for i in range(parts_count)
            ]
            story_type = "user_supplied_story_offline_mapping"
    elif mode == "short":
        part_texts = topic_short_parts(topic or title, parts_count)
        chunks_by_part = [
            split_balanced(part_text, scenes_per_part) for part_text in part_texts
        ]
        story_type = "offline_topic_short_template_not_ai_generated"
    else:
        all_chunks = topic_full_chunks(
            topic or title, parts_count * scenes_per_part
        )
        chunks_by_part = [
            all_chunks[i * scenes_per_part:(i + 1) * scenes_per_part]
            for i in range(parts_count)
        ]
        story_type = "offline_topic_template_not_ai_generated"

    first_scene = chunks_by_part[0][0]
    hook_units = sentences(first_scene)
    hook = hook_units[0] if hook_units else clean(first_scene)

    parts = []
    global_no = 0
    part_counts = []

    for p, plan_part in enumerate(plan["parts"], 1):
        chunks = chunks_by_part[p - 1]
        part_counts.append(word_count(" ".join(chunks)))
        scene_rows = []

        for s, plan_scene in enumerate(plan_part["scenes"], 1):
            global_no += 1
            narration = clean(chunks[s - 1])
            visual = clean(plan_scene.get("visual") or plan_scene.get("visual_prompt"))
            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene showing: "
                    + narration[:450]
                    + ". Natural expressions, realistic skin texture, "
                    "consistent character identity and clothing, realistic "
                    "lighting and exposure, no cartoon style, no exaggerated contrast."
                )

            suspense = ""
            if s == scenes_per_part and p < parts_count:
                part_units = sentences(" ".join(chunks))
                suspense = part_units[-1] if part_units else narration
            elif s == scenes_per_part and p == parts_count:
                suspense = (
                    "Resolve the established central question using supported "
                    "events; do not invent an unrelated reveal."
                )

            scene_rows.append({
                "part": p,
                "scene": s,
                "scene_id": f"part_{p:02d}_scene_{s:02d}",
                "global_scene_number": global_no,
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
            "scenes": scene_rows,
        })

    return {
        "status": "completed",
        "format": mode,
        "title": title,
        "topic": topic or title,
        "source_text": source_text,
        "story_type": story_type,
        "hook": hook,
        "ending_type": "resolution",
        "parts": parts,
        "part_word_counts": part_counts,
        "short_word_target": {
            "minimum": SHORT_MIN_WORDS,
            "maximum": SHORT_MAX_WORDS,
            "target": SHORT_TARGET_WORDS,
            "validation": "strict_120_150_words_per_part",
        } if mode == "short" else None,
        "source_text_sha256": hashlib.sha256(
            clean(source_text).encode("utf-8")
        ).hexdigest(),
        "generation_method": "offline_no_api",
        "mapping_version": 5,
        "_generation": {
            "script_version": VERSION,
            "format": mode,
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


def validate_story(data, mode, title, topic, parts_count, scenes_per_part):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Story root/status is invalid.")
    if clean(data.get("format")).lower() != mode:
        raise RuntimeError("Story FORMAT mismatch.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(f"Expected exactly {parts_count} parts.")

    seen = set()
    global_no = 0
    counts = []

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict) or int(part.get("part", -1)) != p:
            raise RuntimeError(f"Invalid part numbering at {p}.")
        rows = part.get("scenes")
        if not isinstance(rows, list) or len(rows) != scenes_per_part:
            raise RuntimeError(
                f"Part {p} must contain {scenes_per_part} scenes."
            )

        part_words = 0
        for s, scene in enumerate(rows, 1):
            global_no += 1
            scene_id = f"part_{p:02d}_scene_{s:02d}"
            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid scene row: {scene_id}")

            scene["part"] = p
            scene["scene"] = s
            scene["scene_id"] = scene_id
            scene["global_scene_number"] = global_no

            for field in ("purpose", "narration", "visual", "dialogue", "suspense"):
                value = scene.get(field, "")
                if not isinstance(value, str):
                    raise RuntimeError(f"{scene_id}: {field} must be text.")
                scene[field] = value.strip()

            if not scene["narration"] or not scene["visual"]:
                raise RuntimeError(f"{scene_id}: narration or visual is empty.")
            if scene_id in seen:
                raise RuntimeError(f"Duplicate scene ID: {scene_id}")
            seen.add(scene_id)
            part_words += word_count(scene["narration"])

        counts.append(part_words)

    if len(seen) != parts_count * scenes_per_part:
        raise RuntimeError("Scene mapping validation failed.")

    if mode == "short":
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
    mode = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    source_text = clean(get_story_text(config))
    parts_count = positive_int(get_parts(config), "PARTS")
    scenes_per_part = positive_int(get_scenes(config), "SCENES")

    if mode not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")
    if not topic and not source_text:
        raise RuntimeError("Provide TOPIC or STORY_TEXT in Input/topic.txt.")

    title = derive_title(topic, source_text)
    if not title:
        raise RuntimeError("Could not determine story title.")

    plan = validate_plan(
        read_json(PLAN_FILE), mode, topic, parts_count, scenes_per_part
    )

    fingerprint = digest({
        "version": VERSION,
        "format": mode,
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
                    saved, mode, title, topic, parts_count, scenes_per_part
                )
                log("RESUME: matching story checkpoint reused.")
                return
            except (RuntimeError, TypeError, ValueError, KeyError) as exc:
                log("Saved story checkpoint invalid; rebuilding:", exc)

    data = make_story(
        plan, mode, title, topic, source_text, parts_count, scenes_per_part
    )
    data = validate_story(
        data, mode, title, topic, parts_count, scenes_per_part
    )

    write_json(OUTPUT_FILE, data)
    atomic_write(TITLE_FILE, title + "\n")
    atomic_write(FINGERPRINT_FILE, fingerprint + "\n")

    log("STORY BUILD: SUCCESS")
    log("Version:", VERSION)
    log("Title:", title)
    log("Format:", mode)
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Words per part:", data["part_word_counts"])
    log("Story type:", data["story_type"])
    if mode == "short":
        log("Short word-count validation: PASS")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log(f"STORY BUILD ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
