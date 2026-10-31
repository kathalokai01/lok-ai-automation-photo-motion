#!/usr/bin/env python3
"""Katha Lok AI — validated story generation and short-part rewriting."""

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

VERSION = "story-builder-v16-short-rewrite-scene-schema"
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
    """Offline title fallback using concepts sampled across the whole story."""
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
            if len(token) > 2
            and token not in stop_words
            and not token.isdigit()
        ]

        position = index / max(total_units - 1, 1)
        section_weight = 1.0
        if 0.20 <= position <= 0.80:
            section_weight = 1.25
        if position >= 0.80:
            section_weight = 1.35

        for token in set(tokens):
            weights[token] = weights.get(token, 0.0) + section_weight
            first_seen.setdefault(token, index)

    if not weights:
        words = re.findall(r"\w+", clean(text))
        return clean(" ".join(words[-9:]))[:80] or "हिंदी कहानी"

    ranked = sorted(
        weights,
        key=lambda token: (-weights[token], first_seen[token], token),
    )
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
    """Split at sentence boundaries; never silently add filler words."""
    from functools import lru_cache

    units = sentences(text)
    if not units:
        raise RuntimeError("Short story has no usable sentences.")

    sizes = [word_count(item) for item in units]
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
            "per part. The story must be rewritten to meet the target."
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


def call_gemini(prompt, model, api_key):
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
            f"Gemini story generation failed with HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"Gemini story generation failed ({type(exc).__name__})."
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
        raise RuntimeError("Gemini response must be a JSON object.")

    return data


def generate_narrative(config, mode, topic, source_text, parts_count):
    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )

    if not api_key:
        if source_text:
            log("No Gemini key: supplied story will be mapped without rewriting.")
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
            f"Return exactly {parts_count} parts. Every part MUST contain "
            "120-150 Hindi whitespace-separated words, targeting 135. "
            "Rewrite and condense the supplied story when necessary. "
            "Do not merely split an overlong story. Do not pad a short story "
            "with repetition. The first part needs an immediate story-specific "
            "hook. Each non-final part ends on a distinct unresolved danger, "
            "reveal or question. The final part resolves the central conflict."
        )
    else:
        mode_rules = (
            f"Return exactly {parts_count} parts. Start with a powerful "
            "story-specific hook. Escalate stakes through the middle. Use "
            "credible reveals and cause-and-effect. Build suspense before "
            "the climax. The final part resolves the central conflict "
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
meaning before choosing the title. Never title it from only the opening
sentence. If the user supplied no title or topic, create a concise,
memorable Hindi title representing the complete story.

STORY REQUIREMENTS:
- Write natural, engaging Hindi narration, not an outline.
- If source text is supplied, preserve its core events and relationships.
- If only a topic is supplied, create a complete original story.
- Keep character identity, motivation, locations and chronology consistent.
- The opening hook must create immediate curiosity using story-specific facts.
- Do not use a generic hook such as "एक दिन की बात है".
- Build suspense through meaningful clues, risks and consequences.
- Do not repeat the same suspense phrase in every part.
- Every non-final part ends at a genuine unresolved turning point.
- The final part resolves the central conflict clearly and emotionally.
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

    data = call_gemini(prompt, model, api_key)
    title = clean(data.get("title"))
    rows = data.get("parts")

    if not title or not isinstance(rows, list) or len(rows) != parts_count:
        raise RuntimeError(
            "Gemini title or requested part count failed validation."
        )

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

        if mode == "short":
            amount = word_count(text)
            if not SHORT_MIN_WORDS <= amount <= SHORT_MAX_WORDS:
                raise RuntimeError(
                    f"Gemini short part {number} has {amount} words; "
                    "expected 120-150. Story generation rejected."
                )
        narratives.append(text)

    return {
        "title": title[:180],
        "parts": narratives,
        "method": "gemini",
    }

def make_story(config, plan, narrative, source_text, topic, model):
    fmt = str(config.get("FORMAT", "full")).strip().lower()
    parts_count = int(config.get("PARTS", 1))
    scenes_count = int(config.get("SCENES", 1))

    title = clean(narrative.get("title") or topic)
    if not title:
        title = derive_title_from_story(source_text or narrative.get("story_text", ""))

    raw_parts = narrative.get("parts", [])
    if not isinstance(raw_parts, list) or len(raw_parts) != parts_count:
        raise RuntimeError(
            f"Story part mismatch: expected {parts_count}, got "
            f"{len(raw_parts) if isinstance(raw_parts, list) else 0}"
        )

    story_parts = []
    all_scenes = []
    global_scene = 0

    for part_index, raw_part in enumerate(raw_parts, start=1):
        if isinstance(raw_part, dict):
            part_text = clean(
                raw_part.get("text")
                or raw_part.get("story")
                or raw_part.get("narration")
            )
            part_title = clean(raw_part.get("title")) or f"भाग {part_index}"
        else:
            part_text = clean(raw_part)
            part_title = f"भाग {part_index}"

        if not part_text:
            raise RuntimeError(f"Part {part_index} has empty narration")

        sentences = split_sentences(part_text)
        if not sentences:
            raise RuntimeError(f"Part {part_index} has no valid sentences")

        scene_sentences = split_balanced(sentences, scenes_count)
        if len(scene_sentences) != scenes_count:
            raise RuntimeError(
                f"Part {part_index}: expected {scenes_count} scenes, "
                f"got {len(scene_sentences)}"
            )

        part_scenes = []

        for scene_index, scene_lines in enumerate(scene_sentences, start=1):
            global_scene += 1
            narration_text = clean(" ".join(scene_lines))

            if not narration_text:
                raise RuntimeError(
                    f"Empty narration at part {part_index}, scene {scene_index}"
                )

            plan_part = plan["parts"][part_index - 1]
            plan_scenes = plan_part.get("scenes", [])
            plan_scene = (
                plan_scenes[scene_index - 1]
                if scene_index <= len(plan_scenes)
                else {}
            )

            visual = clean(
                plan_scene.get("visual")
                or plan_scene.get("visual_prompt")
                or plan_scene.get("description")
            )
            purpose = clean(plan_scene.get("purpose")) or "कहानी का अगला दृश्य"
            dialogue = clean(plan_scene.get("dialogue"))
            suspense = clean(
                plan_scene.get("suspense")
                or plan_scene.get("suspense_prompt")
            )

            if not visual:
                visual = (
                    "Photorealistic live-action cinematic scene showing: "
                    + narration_text
                    + ". Maintain character and location continuity."
                )

            scene_row = {
                "part": part_index,
                "scene": scene_index,
                "scene_id": f"p{part_index:02d}_s{scene_index:02d}",
                "global_scene_number": global_scene,
                "purpose": purpose,
                "narration": narration_text,
                "visual": visual,
                "dialogue": dialogue,
                "suspense": suspense,
            }

            part_scenes.append(scene_row)
            all_scenes.append(scene_row)

        story_parts.append({
            "part": part_index,
            "title": part_title,
            "narration": part_text,
            "word_count": word_count(part_text),
            "scenes": part_scenes,
        })

    result = {
        "version": VERSION,
        "status": "completed",
        "title": title,
        "topic": clean(topic),
        "format": fmt,
        "model": model,
        "generation_method": narrative.get(
            "generation_method", "gemini_or_offline"
        ),
        "source_fingerprint": digest({
            "source": source_text,
            "topic": topic,
            "format": fmt,
            "parts": parts_count,
            "scenes": scenes_count,
            "model": model,
        }),
        "story_text": "\n\n".join(
            part["narration"] for part in story_parts
        ),
        "total_parts": parts_count,
        "total_scenes": len(all_scenes),
        "parts": story_parts,
        "scenes": all_scenes,
    }

    validate_story(result, parts_count, scenes_count, fmt)
    return result


def validate_story(story, expected_parts, expected_scenes, fmt):
    if story.get("status") != "completed":
        raise RuntimeError("Story status is not completed")

    if not clean(story.get("title")):
        raise RuntimeError("Story title is missing")

    parts = story.get("parts")
    scenes = story.get("scenes")

    if not isinstance(parts, list) or len(parts) != expected_parts:
        raise RuntimeError("Story part count validation failed")

    if not isinstance(scenes, list):
        raise RuntimeError("Story scenes must be a list")

    expected_total = expected_parts * expected_scenes
    if len(scenes) != expected_total:
        raise RuntimeError(
            f"Scene count mismatch: expected {expected_total}, got {len(scenes)}"
        )

    global_index = 0

    for part_index, part in enumerate(parts, start=1):
        if int(part.get("part", 0)) != part_index:
            raise RuntimeError(f"Invalid part numbering at part {part_index}")

        part_scenes = part.get("scenes")
        if not isinstance(part_scenes, list) or len(part_scenes) != expected_scenes:
            raise RuntimeError(
                f"Part {part_index} does not contain exactly "
                f"{expected_scenes} scenes"
            )

        part_text = clean(part.get("narration"))
        if not part_text:
            raise RuntimeError(f"Part {part_index} narration is empty")

        if fmt == "short":
            count = word_count(part_text)
            if not 120 <= count <= 150:
                raise RuntimeError(
                    f"Short part {part_index} has {count} words; "
                    "required range is 120–150"
                )

        for scene_index, scene in enumerate(part_scenes, start=1):
            global_index += 1

            if int(scene.get("part", 0)) != part_index:
                raise RuntimeError("Scene part mapping mismatch")

            if int(scene.get("scene", 0)) != scene_index:
                raise RuntimeError(
                    f"Scene number missing or incorrect: "
                    f"part {part_index}, scene {scene_index}"
                )

            if int(scene.get("global_scene_number", 0)) != global_index:
                raise RuntimeError("Global scene numbering mismatch")

            for field in ("scene_id", "narration", "visual"):
                if not clean(scene.get(field)):
                    raise RuntimeError(
                        f"Missing {field} at part {part_index}, scene {scene_index}"
                    )

    if story.get("total_scenes") != expected_total:
        raise RuntimeError("Story total_scenes metadata mismatch")


def main():
    config_path = Path("Input/topic.txt")
    if not config_path.is_file():
        raise RuntimeError("Missing configuration file: Input/topic.txt")

    try:
        from input_config import load_config
        config = load_config(str(config_path))
    except Exception as exc:
        raise RuntimeError(f"Could not load Input/topic.txt: {exc}") from exc

    fmt = str(config.get("FORMAT", "full")).strip().lower()
    parts_count = int(config.get("PARTS", 1))
    scenes_count = int(config.get("SCENES", 1))
    topic = clean(config.get("TOPIC", ""))
    source_text = clean(config.get("STORY_TEXT", ""))

    if not topic and not source_text:
        raise RuntimeError("Input/topic.txt must contain TOPIC or STORY_TEXT")

    plan_path = Path("output/config/story_plan.json")
    if not plan_path.is_file():
        raise RuntimeError("Missing output/config/story_plan.json")

    plan = read_json(plan_path)
    validate_plan(plan, parts_count, scenes_count)

    model = selected_model()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    fingerprint = digest({
        "version": VERSION,
        "format": fmt,
        "parts": parts_count,
        "scenes": scenes_count,
        "topic": topic,
        "source_text": source_text,
        "model": model,
        "plan": plan,
    })

    output_dir = Path("output/story")
    output_dir.mkdir(parents=True, exist_ok=True)

    story_path = output_dir / "story.json"
    cache_path = output_dir / "story_cache.json"

    if story_path.is_file():
        try:
            cached = read_json(story_path)
            if (
                cached.get("source_fingerprint") == fingerprint
                and cached.get("status") == "completed"
            ):
                validate_story(cached, parts_count, scenes_count, fmt)
                print("Valid completed story found; reusing cached output.")
                return
        except Exception:
            pass

    narrative = generate_narrative(
        config=config,
        plan=plan,
        topic=topic,
        source_text=source_text,
        model=model,
        api_key=api_key,
        fmt=fmt,
        parts_count=parts_count,
    )

    story = make_story(
        config=config,
        plan=plan,
        narrative=narrative,
        source_text=source_text,
        topic=topic,
        model=model,
    )

    story["source_fingerprint"] = fingerprint
    validate_story(story, parts_count, scenes_count, fmt)

    write_json(story_path, story)
    write_json(cache_path, {
        "version": VERSION,
        "source_fingerprint": fingerprint,
        "story": story,
    })

    print(
        f"Story completed: {story['total_parts']} parts, "
        f"{story['total_scenes']} scenes, title={story['title']}"
    )


if __name__ == "__main__":
    main()
