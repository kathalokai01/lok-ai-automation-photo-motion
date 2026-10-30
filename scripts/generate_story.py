#!/usr/bin/env python3
"""Katha Lok AI — story generation with hook and suspense validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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

VERSION = "story-builder-v15-whole-story-title-model-cache"
SHORT_MIN_WORDS = 120
SHORT_MAX_WORDS = 150
SHORT_TARGET_WORDS = 135
MAX_RESPONSE_TOKENS = 16000
REQUEST_TIMEOUT = 120


def log(*items):
    print(*items, flush=True)


def clean(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def word_count(text):
    return len(re.findall(r"\S+", clean(text)))


def digest(data):
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
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
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    result = []
    for block in re.split(r"\n+", text):
        for item in re.split(r"(?<=[।.!?！？])\s+", block.strip()):
            item = clean(item)
            if item:
                result.append(item)
    return result


def derive_title_from_story(text):
    """
    Offline extractive title fallback.
    Uses vocabulary distributed across the complete story and its ending;
    it does not select the first or a single supposedly representative sentence.
    """
    units = sentences(text)
    if not units:
        return "हिंदी कहानी"

    stop_words = {
        "और", "या", "लेकिन", "मगर", "क्योंकि", "इसलिए", "फिर",
        "तब", "जब", "जहाँ", "वहाँ", "जिस", "जिसने", "जिसका",
        "उसने", "उसका", "उसकी", "उसके", "उनके", "उनकी",
        "यह", "वह", "ये", "वे", "इस", "उस", "एक", "कोई",
        "कुछ", "सभी", "बहुत", "काफी", "था", "थी", "थे",
        "है", "हैं", "हो", "हुआ", "हुई", "हुए", "कर",
        "करके", "किया", "किए", "की", "का", "के", "में",
        "पर", "से", "तक", "लिए", "साथ", "अपने", "अपनी",
        "अपना", "नहीं", "नही", "भी", "ही", "तो", "अब",
        "आज", "कल", "वहां", "यहां", "रहा", "रही", "रहे",
        "बाद", "पहले", "सामने", "दौरान", "तरह", "बात",
        "कहानी", "कथा", "औरत", "आदमी",
    }

    weights = {}
    first_seen = {}
    total_units = len(units)

    for index, sentence in enumerate(units):
        normalized = re.sub(r"[^\w\s]", " ", sentence.lower())
        tokens = [
            token for token in normalized.split()
            if len(token) > 2 and token not in stop_words
            and not token.isdigit()
        ]

        if not tokens:
            continue

        # Give words appearing across multiple story sections more weight.
        position = index / max(total_units - 1, 1)
        section_weight = 1.0
        if 0.20 <= position <= 0.80:
            section_weight = 1.25
        if position >= 0.80:
            section_weight = 1.35

        unique_tokens = set(tokens)
        for token in unique_tokens:
            weights[token] = weights.get(token, 0.0) + section_weight
            first_seen.setdefault(token, index)

    if not weights:
        # Last-resort fallback still samples the complete text.
        all_words = re.findall(r"\w+", clean(text))
        candidate_words = all_words[-9:] if len(all_words) > 9 else all_words
        return clean(" ".join(candidate_words))[:80] or "हिंदी कहानी"

    ranked = sorted(
        weights,
        key=lambda token: (
            -weights[token],
            first_seen[token],
            token,
        ),
    )

    # Prefer central story concepts, then add a meaningful ending clue
    # when it is not already represented.
    chosen = ranked[:4]
    ending_tokens = []
    for sentence in units[max(0, int(total_units * 0.75)):]:
        normalized = re.sub(r"[^\w\s]", " ", sentence.lower())
        for token in normalized.split():
            if (
                len(token) > 2
                and token not in stop_words
                and token not in chosen
                and token not in ending_tokens
            ):
                ending_tokens.append(token)

    if ending_tokens and len(chosen) < 5:
        chosen.append(ending_tokens[0])

    # Hindi title assembled from story-wide keywords, not a copied sentence.
    title = " ".join(chosen[:5]).strip()
    title = re.sub(r"\s+", " ", title)
    title = title.strip(" ।.!?！？—-:;,'\"“”‘’")

    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0]

    return title or "हिंदी कहानी"


def split_balanced(text, count):
    units = sentences(text)
    if not units:
        raise RuntimeError("Story has no usable sentences.")
    if count < 1:
        raise RuntimeError("Scene count must be positive.")

    if len(units) < count:
        tokens = re.findall(r"\S+", text)
        if len(tokens) < count:
            raise RuntimeError("Not enough words to create requested scenes.")
        return [
            " ".join(
                tokens[
                    round(i * len(tokens) / count):
                    round((i + 1) * len(tokens) / count)
                ]
            )
            for i in range(count)
        ]

    result = []
    cursor = 0

    for index in range(count):
        left = count - index
        if left == 1:
            result.append(" ".join(units[cursor:]))
            break

        remaining = sum(word_count(x) for x in units[cursor:])
        target = remaining / left
        group = []
        amount = 0
        max_take = len(units) - cursor - (left - 1)

        while cursor < len(units) and len(group) < max_take:
            item = units[cursor]
            size = word_count(item)

            if group and abs(amount - target) <= abs(amount + size - target):
                break

            group.append(item)
            amount += size
            cursor += 1

            if amount >= target:
                break

        if not group:
            group.append(units[cursor])
            cursor += 1

        result.append(" ".join(group))

    if len(result) != count or any(not clean(x) for x in result):
        raise RuntimeError("Could not split story into requested scenes.")

    return result


def split_short_parts(text, count):
    from functools import lru_cache

    units = sentences(text)
    if not units:
        raise RuntimeError("Short story has no usable sentences.")

    sizes = [word_count(x) for x in units]
    prefix = [0]
    for size in sizes:
        prefix.append(prefix[-1] + size)

    total = prefix[-1]
    if not count * SHORT_MIN_WORDS <= total <= count * SHORT_MAX_WORDS:
        raise RuntimeError(
            f"Short story has {total} words for {count} parts. "
            f"Required total: {count * SHORT_MIN_WORDS}-"
            f"{count * SHORT_MAX_WORDS}."
        )

    @lru_cache(maxsize=None)
    def solve(start, left):
        if left == 1:
            amount = prefix[-1] - prefix[start]
            if SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                return (abs(amount - SHORT_TARGET_WORDS), ())
            return None

        best = None
        last_end = len(units) - left + 1

        for end in range(start + 1, last_end + 1):
            amount = prefix[end] - prefix[start]
            if amount < SHORT_MIN_WORDS:
                continue
            if amount > SHORT_MAX_WORDS:
                break

            rest = solve(end, left - 1)
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
            "Cannot split at sentence boundaries into 120-150 words "
            "per part. No filler will be added."
        )

    boundaries = list(solution[1]) + [len(units)]
    result = []
    start = 0

    for end in boundaries:
        result.append(" ".join(units[start:end]).strip())
        start = end

    for number, part in enumerate(result, 1):
        amount = word_count(part)
        if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
            raise RuntimeError(
                f"Part {number} has {amount} words; expected 120-150."
            )

    return result


def validate_plan(plan, mode, topic, parts_count, scenes_per_part):
    if not isinstance(plan, dict) or plan.get("status") != "planned":
        raise RuntimeError("story_plan.json is missing or not marked planned.")

    if clean(plan.get("format")).lower() != mode:
        raise RuntimeError("Story plan format differs from Input/topic.txt.")

    plan_topic = clean(plan.get("topic"))
    if topic and plan_topic and topic != plan_topic:
        raise RuntimeError("Story plan topic differs from current input.")

    total = parts_count * scenes_per_part

    for key, expected in {
        "parts_count": parts_count,
        "scenes_per_part": scenes_per_part,
        "total_scenes": total,
    }.items():
        try:
            actual = int(plan.get(key, -1))
        except (TypeError, ValueError):
            actual = -1
        if actual != expected:
            raise RuntimeError(f"Story plan {key} mismatch.")

    parts = plan.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError("Story plan has the wrong number of parts.")

    global_no = 0
    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != p:
            raise RuntimeError(f"Invalid plan part number: {p}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(f"Plan part {p} has the wrong scene count.")

        for s, scene in enumerate(scenes, 1):
            global_no += 1
            expected_id = f"part_{p:02d}_scene_{s:02d}"

            if not isinstance(scene, dict):
                raise RuntimeError(f"Invalid planned scene: {expected_id}.")
            if scene.get("scene_id") != expected_id:
                raise RuntimeError(f"Plan scene ID mismatch: {expected_id}.")
            if scene.get("scene") != s:
                raise RuntimeError(f"Plan scene number mismatch: {expected_id}.")
            if scene.get("global_scene_number") != global_no:
                raise RuntimeError(f"Plan global number mismatch: {expected_id}.")
            if not clean(scene.get("purpose")):
                raise RuntimeError(f"Plan purpose missing: {expected_id}.")

    return plan


def selected_model():
    path = ROOT / "output" / "config" / "selected_model.json"
    data = read_json(path)
    model = clean(data.get("model")) if data else ""

    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(
            "No valid selected Gemini text model found. "
            "Check the Select Gemini model workflow step."
        )
    return model


def generate_narrative(config, mode, topic, source_text, parts_count):
    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )

    if not api_key:
        if source_text:
            log("No Gemini key: mapping supplied story without rewriting.")
            return {
                "title": topic or derive_title_from_story(source_text),
                "parts": [source_text],
                "method": "user_story_offline_mapping",
            }
        raise RuntimeError(
            "TOPIC-only story generation needs GEMINI_API_KEY or "
            "GOOGLE_API_KEY. No generic template story will be substituted."
        )

    model = selected_model()
    source = source_text if source_text else "(No source story supplied.)"

    if mode == "short":
        mode_rules = (
            f"Return exactly {parts_count} parts. Each part must contain "
            "120-150 Hindi words, targeting 135 words. Build a strong hook "
            "in the first part. Each non-final part must end with a genuine "
            "unresolved story question or danger. The final part must "
            "resolve the central conflict and provide a meaningful ending."
        )
    else:
        mode_rules = (
            f"Return exactly {parts_count} parts. Start with a powerful "
            "story-specific hook. Escalate stakes through the middle. Use "
            "credible reveals and cause-and-effect. Build suspense before "
            "the climax. The final part must resolve the central conflict "
            "without an abrupt or unexplained ending."
        )

    prompt = f"""
You are the Hindi story writer for a cinematic live-action story video.

FORMAT: {mode}
REQUESTED PARTS: {parts_count}
USER TOPIC OR TITLE: {topic or "(no title supplied)"}
AUDIENCE: {clean(str(config.get("AUDIENCE", "adult")))}

COMPLETE SUPPLIED STORY:
<source_story>
{source}
</source_story>

WHOLE-STORY TITLE:
Read the complete supplied story, including its middle and ending.
Identify the central character, conflict, turning point, outcome and
meaning before choosing the title. Do not derive the title from only
the first sentence. If no title is supplied, create a concise memorable
Hindi title representing the complete story. Avoid generic titles.

STORY REQUIREMENTS:
- Write natural, engaging Hindi narration, not an outline.
- If source text is supplied, preserve its core events and relationships.
- If only a topic is supplied, create a complete original story.
- Keep character identity, motivation, locations and chronology consistent.
- Opening hook must create immediate curiosity using story-specific facts.
- Do not use a generic hook such as "एक दिन की बात है".
- Build suspense through meaningful clues, risks and consequences.
- Do not repeat the same suspense phrase in every part.
- Every non-final part must end at a genuine unresolved turning point.
- The final part must resolve the central conflict clearly and emotionally.
- Do not end the final story with an artificial cliffhanger.
- Do not add visual prompts or scene directions to narration.
- Do not create a second format.
- Use Hindi Devanagari unless the source clearly requires another language.

FORMAT RULES:
{mode_rules}

Return valid JSON only:
{{
  "title": "complete-story title",
  "parts": [
    {{"part": 1, "text": "complete narration"}}
  ]
}}
No markdown fences and no extra keys.
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model, safe="-._")
        + ":generateContent"
    )

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.75,
            "maxOutputTokens": MAX_RESPONSE_TOKENS,
        },
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
        except Exception:
            pass
        raise RuntimeError(
            f"Gemini story generation failed with HTTP {exc.code}. "
            f"No automatic retry was attempted. {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"Gemini story generation failed ({type(exc).__name__}). "
            "No automatic retry was attempted."
        ) from exc

    candidates = result.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini returned no story candidate.")

    response_parts = candidates[0].get("content", {}).get("parts", [])
    response_text = "".join(
        item.get("text", "")
        for item in response_parts
        if isinstance(item, dict)
    ).strip()

    if not response_text:
        raise RuntimeError("Gemini returned an empty story response.")

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini response was not valid JSON.") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Gemini story response must be a JSON object.")

    title = clean(data.get("title"))
    rows = data.get("parts")

    if not title or not isinstance(rows, list) or len(rows) != parts_count:
        raise RuntimeError("Gemini title or requested part count failed validation.")

    narratives = []
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise RuntimeError(f"Gemini returned invalid part {number}.")
        try:
            returned_number = int(row.get("part", -1))
        except (TypeError, ValueError):
            returned_number = -1
        if returned_number != number:
            raise RuntimeError(f"Gemini returned invalid part {number}.")

        text = clean(row.get("text"))
        if not text:
            raise RuntimeError(f"Gemini returned empty part {number}.")
        narratives.append(text)

    return {
        "title": title[:180],
        "parts": narratives,
        "method": "gemini",
    }

def make_story(
    plan,
    mode,
    topic,
    source_text,
    narrative,
    parts_count,
    scenes_per_part,
):
    title = (
        clean(narrative.get("title"))
        or topic
        or derive_title_from_story(source_text)
    )

    part_texts = narrative["parts"]

    if len(part_texts) == 1 and parts_count > 1:
        if mode == "short":
            part_texts = split_short_parts(part_texts[0], parts_count)
        else:
            all_scenes = split_balanced(
                part_texts[0], parts_count * scenes_per_part
            )
            part_texts = [
                " ".join(
                    all_scenes[
                        i * scenes_per_part:(i + 1) * scenes_per_part
                    ]
                )
                for i in range(parts_count)
            ]

    if len(part_texts) != parts_count:
        raise RuntimeError("Narrative part count does not match PARTS.")

    if mode == "short":
        for number, text in enumerate(part_texts, 1):
            amount = word_count(text)
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"Short part {number} has {amount} words; expected 120-150."
                )

    result_parts = []
    word_counts = []
    global_no = 0

    for p, plan_part in enumerate(plan["parts"], 1):
        text = part_texts[p - 1]
        scene_texts = split_balanced(text, scenes_per_part)
        word_counts.append(word_count(text))
        scenes = []

        for s, (narration, plan_scene) in enumerate(
            zip(scene_texts, plan_part["scenes"]), 1
        ):
            global_no += 1
            scene_id = f"part_{p:02d}_scene_{s:02d}"

            visual = clean(
                plan_scene.get("visual") or plan_scene.get("visual_prompt")
            )
            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene depicting: "
                    + narration[:450]
                    + ". Natural skin texture, realistic proportions, "
                    "coherent action, natural exposure, consistent character "
                    "identity, no cartoon style, no text or watermark."
                )

            is_part_end = s == scenes_per_part
            suspense = ""

            if is_part_end and p < parts_count:
                final_sentences = sentences(narration)
                suspense = final_sentences[-1] if final_sentences else narration
            elif is_part_end and p == parts_count:
                suspense = (
                    "Resolve the central story conflict using established events."
                )

            scenes.append({
                "part": p,
                "scene": s,
                "scene_id": scene_id,
                "global_scene_number": global_no,
                "purpose": clean(plan_scene.get("purpose"))
                or "कहानी को आगे बढ़ाना",
                "narration": narration,
                "visual": visual,
                "dialogue": "",
                "suspense": suspense,
            })

        result_parts.append({
            "part": p,
            "part_id": f"part_{p:02d}",
            "title": clean(plan_part.get("title")) or f"भाग {p}",
            "scenes": scenes,
        })

    first_scene = result_parts[0]["scenes"][0]["narration"]
    first_units = sentences(first_scene)
    hook = first_units[0] if first_units else first_scene

    return {
        "status": "completed",
        "format": mode,
        "title": title,
        "topic": topic or title,
        "source_text": source_text,
        "story_type": narrative.get("method", "generated"),
        "hook": hook,
        "ending_type": "resolution",
        "parts": result_parts,
        "part_word_counts": word_counts,
        "short_word_target": {
            "minimum": SHORT_MIN_WORDS,
            "maximum": SHORT_MAX_WORDS,
            "target": SHORT_TARGET_WORDS,
            "validation": "strict_120_150_words_per_part",
        } if mode == "short" else None,
        "source_text_sha256": hashlib.sha256(
            clean(source_text).encode("utf-8")
        ).hexdigest(),
        "generation_method": narrative.get("method", "unknown"),
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
            "api_calls": 1 if narrative.get("method") == "gemini" else 0,
        },
    }


def validate_story(data, mode, title, topic, parts_count, scenes_per_part):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise RuntimeError("Story root/status is invalid.")

    if clean(data.get("format")).lower() != mode:
        raise RuntimeError("Story format mismatch.")

    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(f"Expected exactly {parts_count} story parts.")

    global_no = 0
    counts = []
    seen = set()

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != p:
            raise RuntimeError(f"Invalid story part number: {p}.")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != scenes_per_part:
            raise RuntimeError(f"Part {p} has an incorrect scene count.")

        amount = 0

        for s, scene in enumerate(scenes, 1):
            global_no += 1
            expected_id = f"part_{p:02d}_scene_{s:02d}"

            if (
                not isinstance(scene, dict)
                or scene.get("scene_id") != expected_id
            ):
                raise RuntimeError(f"Invalid story scene: {expected_id}.")

            if (
                scene.get("scene") != s
                or scene.get("global_scene_number") != global_no
            ):
                raise RuntimeError(f"Incorrect scene numbering: {expected_id}.")

            for key in (
                "purpose", "narration", "visual", "dialogue", "suspense"
            ):
                if not isinstance(scene.get(key, ""), str):
                    raise RuntimeError(f"{expected_id}: {key} must be text.")

            if not clean(scene.get("narration")) or not clean(scene.get("visual")):
                raise RuntimeError(
                    f"{expected_id}: narration or visual is empty."
                )

            if expected_id in seen:
                raise RuntimeError(f"Duplicate scene ID: {expected_id}.")

            seen.add(expected_id)
            amount += word_count(scene["narration"])

        counts.append(amount)

    if mode == "short":
        for p, amount in enumerate(counts, 1):
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"Short part {p} has {amount} narration words; "
                    "expected 120-150."
                )

    first_scene = parts[0]["scenes"][0]
    hook = clean(data.get("hook"))
    if not hook:
        raise RuntimeError("Opening hook is missing.")

    if hook not in clean(first_scene.get("narration")):
        raise RuntimeError("Opening hook does not match first-scene narration.")

    for p, part in enumerate(parts, 1):
        last_scene = part["scenes"][-1]
        suspense = clean(last_scene.get("suspense"))

        if p < parts_count and not suspense:
            raise RuntimeError(
                f"Part {p} is missing its ending suspense marker."
            )

    final_scene = parts[-1]["scenes"][-1]
    if not clean(final_scene.get("narration")):
        raise RuntimeError("Final story ending narration is empty.")

    data["title"] = clean(data.get("title")) or title
    data["topic"] = topic or data["title"]
    data["part_word_counts"] = counts
    data["validation"] = {
        "exact_part_count": True,
        "exact_scene_count": True,
        "unique_scene_ids": True,
        "opening_hook_present": True,
        "part_end_suspense_markers_present": True,
        "final_narration_present": True,
        "short_word_limits_verified": mode == "short",
    }
    return data


def main():
    log("=" * 58)
    log("KATHA LOK AI — STORY GENERATOR")
    log("Version:", VERSION)

    config = load_and_validate(ROOT / "Input" / "topic.txt")
    mode = clean(get_format(config)).lower()
    topic = clean(get_topic(config))
    source_text = clean(get_story_text(config))
    parts_count = int(get_parts(config))
    scenes_per_part = int(get_scenes(config))

    if mode not in {"short", "full"}:
        raise RuntimeError("FORMAT must be short or full.")

    if parts_count < 1 or scenes_per_part < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")

    if not topic and not source_text:
        raise RuntimeError("Provide TOPIC or STORY_TEXT in Input/topic.txt.")

    plan = validate_plan(
        read_json(PLAN_FILE),
        mode,
        topic,
        parts_count,
        scenes_per_part,
    )

    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )

    # Model selection is part of the cache identity. A model change must
    # invalidate a story generated by a different model.
    model_id = selected_model() if api_key else "offline-user-story"

    generation_settings = {
        "audience": clean(str(config.get("AUDIENCE", "adult"))),
        "temperature": 0.75,
        "max_output_tokens": MAX_RESPONSE_TOKENS,
        "model": model_id,
    }

    fingerprint = digest({
        "version": VERSION,
        "format": mode,
        "topic": topic,
        "source_text": source_text,
        "parts": parts_count,
        "scenes_per_part": scenes_per_part,
        "plan": plan,
        "generation_settings": generation_settings,
    })

    try:
        saved_fingerprint = FINGERPRINT_FILE.read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        saved_fingerprint = ""

    if saved_fingerprint == fingerprint:
        saved = read_json(OUTPUT_FILE)
        if saved:
            try:
                validate_story(
                    saved,
                    mode,
                    clean(saved.get("title")),
                    topic,
                    parts_count,
                    scenes_per_part,
                )
                log("RESUME: matching validated story reused; no API call.")
                return
            except (RuntimeError, TypeError, ValueError, KeyError) as exc:
                log("Saved story checkpoint invalid; rebuilding:", exc)

    narrative = generate_narrative(
        config, mode, topic, source_text, parts_count
    )

    title = (
        clean(narrative.get("title"))
        or topic
        or derive_title_from_story(source_text)
    )

    data = make_story(
        plan,
        mode,
        topic,
        source_text,
        narrative,
        parts_count,
        scenes_per_part,
    )

    data = validate_story(
        data,
        mode,
        title,
        topic,
        parts_count,
        scenes_per_part,
    )

    write_json(OUTPUT_FILE, data)
    atomic_write(TITLE_FILE, data["title"] + "\n")
    atomic_write(FINGERPRINT_FILE, fingerprint + "\n")

    log("STORY GENERATION: SUCCESS")
    log("Version:", VERSION)
    log("Format:", mode)
    log("Title:", data["title"])
    log("Parts:", parts_count)
    log("Scenes per part:", scenes_per_part)
    log("Narration words per part:", data["part_word_counts"])
    log("Generation method:", data["generation_method"])
    log("Validation:", data["validation"])
    log("External story-generation requests:", data["_generation"]["api_calls"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
    except Exception as exc:
        log("STORY GENERATION ERROR:", exc, file=sys.stderr)
        sys.exit(1)