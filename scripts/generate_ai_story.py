#!/usr/bin/env python3
"""Generate story-grounded AI narration with configured Gemini fallback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from input_config import (
    load_input_config,
    normalize_format,
    get_max_retries,
    get_topic,
    get_story_text,
)
from select_gemini_model import configured_fallbacks

ROOT = Path(__file__).resolve().parent.parent
STORY_FILE = ROOT / "output" / "story" / "story.json"
PLAN_FILE = ROOT / "output" / "story" / "story_plan.json"
OUTPUT_FILE = ROOT / "output" / "story" / "ai_story.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
REQUEST_TIMEOUT = 180
MAX_RESPONSE_CHARS = 160000
VERSION = "ai-story-v16-selector-fallback-runtime"

SHORT_MIN_WORDS = 120
SHORT_TARGET_MAX = 150
SHORT_HARD_MAX = 180
SHORT_FINAL_MERGED_MAX = 239

ROLE_KEYS = {
    "hook_scene": "hook",
    "suspense_scene": "suspense",
    "resolution_scene": "resolution",
}

REQUIRED_FIELDS = (
    "narration",
    "dialogue",
    "visual_prompt",
    "negative_prompt",
    "camera_prompt",
    "lighting_prompt",
    "sfx_prompt",
    "music_prompt",
)

REQUIRED_NONEMPTY = (
    "narration",
    "visual_prompt",
    "camera_prompt",
    "lighting_prompt",
)


def log(message: str = "") -> None:
    print(message, flush=True)


def clean(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def word_count(value: str) -> int:
    return len(re.findall(r"\S+", clean(value)))


def stable_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path, required: bool = True):
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"Required file missing: {path}")
        return None

    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")

    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_bool(config: dict, key: str, default: bool) -> bool:
    value = config.get(key, default)

    if isinstance(value, bool):
        return value
    if value is None:
        return default

    text = str(value).strip().lower()

    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False

    return default


def build_config() -> dict:
    config = load_input_config()

    return {
        "format": normalize_format(config),
        "audience": clean(config.get("AUDIENCE", "adult")) or "adult",
        "story_length": clean(config.get("STORY_LENGTH", "auto")) or "auto",
        "scene_duration": clean(config.get("SCENE_DURATION", "auto")) or "auto",
        "visual_style": clean(config.get("VISUAL_STYLE", "cinematic_realistic")),
        "realism": clean(config.get("REALISM", "high")),
        "camera_style": clean(config.get("CAMERA_STYLE", "cinematic")),
        "lighting": clean(config.get("LIGHTING", "cinematic")),
        "mood": clean(config.get("MOOD", "dramatic")),
        "quality": clean(config.get("QUALITY", "high")),
        "music": parse_bool(config, "MUSIC", True),
        "music_style": clean(config.get("MUSIC_STYLE", "cinematic")),
        "sfx": parse_bool(config, "SFX", True),
        "ambient_sound": parse_bool(config, "AMBIENT_SOUND", True),
        "negative_prompt": parse_bool(config, "NEGATIVE_PROMPT", True),
        "avoid_cartoon": parse_bool(config, "AVOID_CARTOON_LOOK", True),
        "avoid_neon": parse_bool(config, "AVOID_NEON", True),
        "avoid_glitch": parse_bool(config, "AVOID_GLITCH_EFFECTS", True),
        "natural_motion": parse_bool(config, "NATURAL_MOTION", True),
        "realistic_lighting": parse_bool(config, "REALISTIC_LIGHTING", True),
        "max_retries": max(1, int(get_max_retries(config))),
        "resume_enabled": parse_bool(config, "RESUME_ENABLED", True),
        "skip_completed": parse_bool(config, "SKIP_COMPLETED_SCENES", True),
    }


def normalize_model(value: Any) -> str:
    model = clean(value)

    if model.startswith("models/"):
        model = model[len("models/"):]

    if not re.fullmatch(r"gemini-[A-Za-z0-9._-]+", model, re.IGNORECASE):
        return ""

    return model


def load_model_order() -> list[str]:
    data = read_json(MODEL_FILE)

    if data.get("status") != "selected":
        raise RuntimeError("Selected Gemini model is not marked selected.")

    primary = normalize_model(data.get("model"))
    if not primary:
        raise RuntimeError("Selected Gemini model name is invalid.")

    # Selector is the source of truth for configured priority and defaults.
    # It also preserves saved custom fallback order when env vars are empty.
    configured_order = configured_fallbacks(data)

    order = []
    for value in [primary, *configured_order]:
        model = normalize_model(value)
        if model and model not in order:
            order.append(model)

    # Retain any explicitly saved order entries without changing their order.
    saved_order = data.get("fallback_order", [])
    if isinstance(saved_order, list):
        for value in saved_order:
            model = normalize_model(value)
            if model and model not in order:
                order.append(model)

    # Explicit workflow fallbacks are appended only if not already present.
    raw_env_fallbacks = os.environ.get("GEMINI_MODEL_FALLBACKS", "")
    for value in re.split(r"[,;]", raw_env_fallbacks):
        model = normalize_model(value)
        if model and model not in order:
            order.append(model)

    if not order:
        raise RuntimeError("No valid configured Gemini model is available.")

    log("Gemini model order:")
    for index, model in enumerate(order, 1):
        log(f"  {index}. {model}")

    return order


def validate_source_extra(
    record: Any,
    expected_role: str,
    part_number: int,
) -> None:
    if not isinstance(record, dict):
        raise RuntimeError(
            f"Part {part_number}: invalid {expected_role} source record."
        )

    if clean(record.get("role")).lower() != expected_role:
        raise RuntimeError(
            f"Part {part_number}: {expected_role} source role mismatch."
        )

    if not clean(record.get("scene_id")):
        raise RuntimeError(
            f"Part {part_number}: {expected_role} has no scene_id."
        )

    if not clean(record.get("story_anchor")):
        raise RuntimeError(
            f"Part {part_number}: {expected_role} has no story_anchor."
        )


def validate_source_plan(plan: dict, expected_format: str):
    if plan.get("status") not in {"planned", "completed", "in_progress"}:
        raise RuntimeError(
            f"Invalid story-plan status: {plan.get('status')!r}"
        )

    if clean(plan.get("format")).lower() != expected_format:
        raise RuntimeError(
            "Story-plan format does not match Input/topic.txt."
        )

    parts = plan.get("parts")

    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Story plan has no parts.")

    if plan.get("parts_count") not in (None, len(parts)):
        raise RuntimeError("Story-plan parts_count mismatch.")

    all_ids = set()
    total_main_scenes = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != part_number:
            raise RuntimeError(
                f"Invalid source part numbering: {part_number}"
            )

        scenes = part.get("scenes")

        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(
                f"Part {part_number} has no main scenes."
            )

        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Part {part_number}, scene {scene_number}: invalid record."
                )

            if scene.get("scene") != scene_number:
                raise RuntimeError(
                    f"Part {part_number}: main scene order mismatch."
                )

            scene_id = clean(scene.get("scene_id"))

            if not scene_id or scene_id in all_ids:
                raise RuntimeError(
                    f"Missing or duplicate source scene_id: {scene_id!r}"
                )

            if clean(scene.get("role")).lower() not in {"", "main"}:
                raise RuntimeError(
                    f"Main scene has an invalid role: {scene_id}"
                )

            if not clean(scene.get("story_anchor")):
                raise RuntimeError(
                    f"Main scene has no story_anchor: {scene_id}"
                )

            all_ids.add(scene_id)

        total_main_scenes += len(scenes)
        final_part = part_number == len(parts)

        for key, role in ROLE_KEYS.items():
            record = part.get(key)
            should_exist = (
                key == "hook_scene"
                or (key == "suspense_scene" and not final_part)
                or (key == "resolution_scene" and final_part)
            )

            if should_exist:
                validate_source_extra(record, role, part_number)
                scene_id = clean(record.get("scene_id"))

                if scene_id in all_ids:
                    raise RuntimeError(
                        f"Duplicate source scene_id: {scene_id}"
                    )

                all_ids.add(scene_id)
            elif record is not None:
                raise RuntimeError(
                    f"Unexpected {key} in part {part_number}."
                )

    if plan.get("total_scenes") not in (None, total_main_scenes):
        raise RuntimeError("Story-plan total_scenes mismatch.")

    return len(parts), total_main_scenes


def apply_story_part_metadata(source_plan: dict, source_story: dict) -> None:
    story_parts = source_story.get("parts")

    if not isinstance(story_parts, list):
        raise RuntimeError("Corrected story has no valid parts list.")

    plan_parts = source_plan["parts"]

    if len(story_parts) != len(plan_parts):
        raise RuntimeError("Story and Story Plan have different part counts.")

    for index, (story_part, plan_part) in enumerate(
        zip(story_parts, plan_parts), 1
    ):
        if not isinstance(story_part, dict):
            raise RuntimeError(f"Invalid corrected story part {index}.")

        if story_part.get("part") != index:
            raise RuntimeError(f"Invalid corrected story part number {index}.")

        merged = story_part.get("final_part_merged") is True

        if merged and index != len(plan_parts):
            raise RuntimeError(
                "final_part_merged may only be true for the final part."
            )

        plan_part["final_part_merged"] = merged


def scene_context(part: dict) -> list:
    return [
        {
            "scene": item["scene"],
            "scene_id": clean(item.get("scene_id")),
            "role": "main",
            "beat": clean(item.get("beat")),
            "purpose": clean(item.get("purpose")),
            "story_anchor": clean(item.get("story_anchor")),
            "visual": clean(item.get("visual") or item.get("visual_prompt")),
        }
        for item in part["scenes"]
    ]


def extra_context(part: dict, key: str):
    record = part.get(key)
    if record is None:
        return None

    return {
        "scene_id": clean(record.get("scene_id")),
        "role": clean(record.get("role")),
        "purpose": clean(record.get("purpose")),
        "story_anchor": clean(record.get("story_anchor")),
        "visual": clean(record.get("visual") or record.get("visual_prompt")),
    }


def short_word_limits(source_part: dict, config: dict, total_parts: int):
    if config["format"] != "short":
        return None, None

    is_final = source_part["part"] == total_parts
    is_merged_final = (
        is_final and source_part.get("final_part_merged") is True
    )

    if is_merged_final:
        return SHORT_MIN_WORDS, SHORT_FINAL_MERGED_MAX
    if is_final:
        return SHORT_MIN_WORDS, SHORT_HARD_MAX
    return SHORT_MIN_WORDS, SHORT_TARGET_MAX


def build_response_schema(source_part: dict) -> dict:
    scene_properties = {
        "scene": {"type": "integer"},
        "scene_id": {"type": "string"},
        "role": {"type": "string"},
    }

    for field in REQUIRED_FIELDS:
        scene_properties[field] = {"type": "string"}

    scene_schema = {
        "type": "object",
        "properties": scene_properties,
        "required": ["scene", "scene_id", "role", *REQUIRED_FIELDS],
    }

    extra_properties = {
        "scene_id": {"type": "string"},
        "role": {"type": "string"},
    }

    for field in REQUIRED_FIELDS:
        extra_properties[field] = {"type": "string"}

    extra_schema = {
        "type": "object",
        "properties": extra_properties,
        "required": ["scene_id", "role", *REQUIRED_FIELDS],
    }

    properties = {
        "scenes": {"type": "array", "items": scene_schema},
        "story_title": {"type": "string"},
    }
    required = ["scenes", "story_title"]

    for key in ROLE_KEYS:
        if source_part.get(key) is not None:
            properties[key] = extra_schema
            required.append(key)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def extract_response_text(result: dict) -> str:
    candidates = result.get("candidates")

    if not isinstance(candidates, list) or not candidates:
        raise ValueError(
            "Gemini returned no candidates: "
            + str(result.get("promptFeedback", {}))
        )

    candidate = candidates[0]

    if candidate.get("finishReason") == "SAFETY":
        raise ValueError("Gemini blocked the generated content.")

    parts = candidate.get("content", {}).get("parts", [])
    text = "\n".join(
        item["text"]
        for item in parts
        if isinstance(item, dict)
        and isinstance(item.get("text"), str)
    ).strip()

    if not text:
        raise ValueError("Gemini returned empty content.")

    if len(text) > MAX_RESPONSE_CHARS:
        raise ValueError("Gemini response exceeded the size limit.")

    return text


def parse_json_response(response_text: str) -> dict:
    value = response_text.strip()
    value = re.sub(
        r"^\s*```(?:json)?\s*|\s*```\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()

    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")

        if start < 0 or end <= start:
            raise ValueError("Model response contains no JSON object.")

        result = json.loads(value[start:end + 1])

    if not isinstance(result, dict):
        raise ValueError("Model response JSON root must be an object.")

    return result


def request_gemini(
    api_key: str,
    model: str,
    prompt: str,
    schema: dict,
) -> dict:
    url = f"{API_BASE}/models/{model}:generateContent"
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}],
        }],
        "generationConfig": {
            "temperature": 0.65,
            "topP": 0.90,
            "responseMimeType": "application/json",
            "responseSchema": schema,
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

    with urllib.request.urlopen(
        request,
        timeout=REQUEST_TIMEOUT,
    ) as response:
        raw = response.read().decode("utf-8")

    return parse_json_response(
        extract_response_text(json.loads(raw))
    )


def build_part_prompt(
    topic: str,
    story_text: str,
    part: dict,
    config: dict,
    total_parts: int,
) -> str:
    number = part["part"]
    final_part = number == total_parts
    merged_final = (
        final_part and part.get("final_part_merged") is True
    )

    minimum, maximum = short_word_limits(part, config, total_parts)

    if config["format"] == "short":
        if merged_final:
            format_rules = f"""
SHORT FINAL MERGED PART:
- Main narration must contain {minimum}–{maximum} words.
- The final merged part may contain up to 239 words.
- Preserve all remaining story events.
- Hook and Resolution are separate records and are not counted.
"""
        elif final_part:
            format_rules = """
SHORT FINAL PART:
- Main narration must contain 120–180 words.
- Preserve the complete ending.
- Do not invent words or events to reach a target.
- Hook and Resolution are separate records and are not counted.
"""
        else:
            format_rules = """
SHORT NON-FINAL PART:
- Main narration target is 120–150 words.
- 151–180 words require revision toward 120–150.
- Below 120 or above 180 words is invalid.
- Count only main narration, excluding Hook and Suspense.
"""
    else:
        format_rules = """
FULL:
- Preserve all important events and emotional developments.
- Do not summarize away important story content.
- Preserve the original ending and event order.
"""

    hook = extra_context(part, "hook_scene")
    suspense = None if final_part else extra_context(part, "suspense_scene")
    resolution = extra_context(part, "resolution_scene") if final_part else None

    return f"""
आप हिंदी कहानी-संपादन और सिनेमैटिक नैरेशन इंजन हैं।

कहानी का विषय: {topic}

पूरी मूल और संशोधित कहानी:
{story_text}

भाग: {number}/{total_parts}
फॉर्मेट: {config['format']}
दर्शक: {config['audience']}
कहानी अवधि: {config['story_length']}
दृश्य अवधि: {config['scene_duration']}

इस भाग के मुख्य दृश्यों का निश्चित क्रम:
{json.dumps(scene_context(part), ensure_ascii=False, indent=2)}

अलग Hook source:
{json.dumps(hook, ensure_ascii=False, indent=2)}

अलग Suspense source:
{json.dumps(suspense, ensure_ascii=False, indent=2)}

अलग Final Resolution source:
{json.dumps(resolution, ensure_ascii=False, indent=2)}

{format_rules}

कहानी के नियम:
- पूरी मूल कहानी को पढ़कर घटनाओं का क्रम, पात्र, नाम, संबंध, स्थान,
  कारण और परिणाम सुरक्षित रखें।
- कहानी का अर्थ, संदेश और मूल अंत न बदलें।
- हिंदी मात्रा, वर्तनी, व्याकरण, लिंग, वचन और काल ठीक करें।
- अधूरे वाक्य, गलत विराम चिह्न, दोहराव और अस्पष्ट सर्वनाम सुधारें।
- कोई नई मुख्य घटना, पात्र या असंबंधित मोड़ न जोड़ें।
- प्रत्येक scene narration उसके story_anchor से मेल खाए।
- scene number, scene_id और scene order बिल्कुल न बदलें।
- मुख्य narration में Hook, Suspense या Resolution का पाठ न मिलाएँ।
- संवाद पात्रों और परिस्थिति के अनुरूप हों।
- भागों के बीच कथा का प्रवाह बना रहे।
- कहानी के मूल शब्दों और घटनाओं को अनावश्यक रूप से न हटाएँ।
- दृश्य में कहानी के वास्तविक नाम, स्थान, वस्तु, पशु, वाहन,
  कपड़े और अन्य कहानी-विशिष्ट तत्व शामिल करें।
- visual_prompt उसी narration के वास्तविक क्षण को दिखाए।
- recurring पात्रों की पहचान और रूप सभी भागों में एक जैसे रखें।
- असंबंधित पात्र, वस्तु, घटना, text, watermark या logo न जोड़ें।
- वाक्य को बीच में न काटें; पूर्ण भाव और विराम के बाद ही विभाजन करें।

HOOK:
- source में मौजूद हो तो अलग hook_scene record दें।
- कहानी-विशिष्ट वास्तविक रहस्य या जिज्ञासा से शुरुआत करें।
- झूठा clickbait या कहानी से बाहर की घटना न गढ़ें।

SUSPENSE:
- केवल non-final part में और source record मौजूद होने पर दें।
- वास्तविक घटना और अगले भाग के प्रश्न से जुड़ा हो।
- अंतिम भाग में suspense record नहीं होना चाहिए।

FINAL RESOLUTION:
- केवल अंतिम part में और source record मौजूद होने पर दें।
- मूल संघर्ष और कहानी का वास्तविक समाधान दें।
- नया अंत या नया अनसुलझा संघर्ष न जोड़ें।

शीर्षक:
- पूरी कहानी के सार, संघर्ष और परिणाम के आधार पर एक शीर्षक दें।
- सभी भागों में story_title बिल्कुल समान रखें।

VISUAL:
- live-action cinematic realism रखें; cartoon/anime न दें।
- Style: {config['visual_style']}
- Realism: {config['realism']}
- Camera: {config['camera_style']}
- Lighting: {config['lighting']}
- Mood: {config['mood']}
- Quality: {config['quality']}
- Avoid cartoon: {config['avoid_cartoon']}
- Avoid neon: {config['avoid_neon']}
- Avoid glitch: {config['avoid_glitch']}
- Natural motion: {config['natural_motion']}
- Realistic lighting: {config['realistic_lighting']}

AUDIO:
- Music enabled: {config['music']}
- Music style: {config['music_style']}
- SFX enabled: {config['sfx']}
- Ambient enabled: {config['ambient_sound']}

हर main scene में ये fields अनिवार्य हैं:
scene, scene_id, role, narration, dialogue, visual_prompt,
negative_prompt, camera_prompt, lighting_prompt, sfx_prompt, music_prompt.

हर उपलब्ध extra record में ये fields अनिवार्य हैं:
scene_id, role, narration, dialogue, visual_prompt,
negative_prompt, camera_prompt, lighting_prompt, sfx_prompt, music_prompt.

Extra record का scene_id और role source plan से हूबहू मिलना चाहिए।
जो extra source record मौजूद नहीं है, उसका output record न दें।
केवल JSON लौटाएँ।
"""


def validate_text_fields(record: Any, label: str) -> dict:
    if not isinstance(record, dict):
        raise ValueError(f"{label} must be a JSON object.")

    result = {}

    for field in REQUIRED_FIELDS:
        value = record.get(field)

        if not isinstance(value, str):
            raise ValueError(f"{label}.{field} must be text.")

        value = value.strip()

        if field in REQUIRED_NONEMPTY and not value:
            raise ValueError(f"{label}.{field} cannot be empty.")

        result[field] = value

    unexpected = set(record) - set(REQUIRED_FIELDS) - {
        "scene", "scene_id", "role"
    }

    if unexpected:
        raise ValueError(
            f"{label} contains unexpected fields: "
            + ", ".join(sorted(unexpected))
        )

    return result


def validate_generated_scenes(
    generated: Any,
    source_part: dict,
    config: dict,
    total_parts: int,
) -> list:
    if not isinstance(generated, list):
        raise ValueError("Generated main scenes must be a list.")

    expected = source_part["scenes"]

    if len(generated) != len(expected):
        raise ValueError(
            f"Part {source_part['part']}: expected {len(expected)} "
            f"main scenes, received {len(generated)}."
        )

    result = []

    for source, item in zip(expected, generated):
        if not isinstance(item, dict):
            raise ValueError("A generated main scene is not an object.")

        if item.get("scene") != source.get("scene"):
            raise ValueError("Generated main scene number/order mismatch.")

        if clean(item.get("scene_id")) != clean(source.get("scene_id")):
            raise ValueError(
                f"Main scene_id mismatch: {source.get('scene_id')}"
            )

        if clean(item.get("role")).lower() != "main":
            raise ValueError(
                f"Main scene has wrong role: {source.get('scene_id')}"
            )

        fields = validate_text_fields(
            item,
            f"Part {source_part['part']} scene {source['scene']}",
        )

        row = {
            "scene": source["scene"],
            "scene_id": clean(source["scene_id"]),
            "role": "main",
            "status": "completed",
            "story_anchor": clean(source.get("story_anchor")),
            "purpose": clean(source.get("purpose")),
            "beat": clean(source.get("beat")),
            "scene_duration": clean(
                source.get("scene_duration", source.get("duration", ""))
            ),
        }
        row.update(fields)
        result.append(row)

    count = word_count(" ".join(row["narration"] for row in result))
    minimum, maximum = short_word_limits(
        source_part, config, total_parts
    )

    if minimum is not None and not minimum <= count <= maximum:
        raise ValueError(
            f"SHORT part {source_part['part']} has {count} main words; "
            f"allowed range is {minimum}–{maximum}."
        )

    if (
        config["format"] == "short"
        and source_part["part"] < total_parts
        and count > SHORT_TARGET_MAX
    ):
        raise ValueError(
            f"SHORT non-final part {source_part['part']} has {count} words; "
            "revise it to 120–150 words."
        )

    return result


def validate_extra_record(
    generated: Any,
    source: Any,
    role: str,
    part_number: int,
):
    if source is None:
        if generated is not None:
            raise ValueError(
                f"Unexpected {role} record in part {part_number}."
            )
        return None

    if not isinstance(generated, dict):
        raise ValueError(
            f"Required separate {role} record missing in part {part_number}."
        )

    if clean(generated.get("scene_id")) != clean(source.get("scene_id")):
        raise ValueError(
            f"{role} scene_id does not match the final Story Plan "
            f"in part {part_number}."
        )

    if clean(generated.get("role")).lower() != role:
        raise ValueError(f"{role} role mismatch in part {part_number}.")

    fields = validate_text_fields(
        generated,
        f"Part {part_number} {role}",
    )

    result = {
        "scene_id": clean(source["scene_id"]),
        "role": role,
        "status": "completed",
        "purpose": clean(source.get("purpose")),
        "story_anchor": clean(source.get("story_anchor")),
        "scene_duration": clean(
            source.get("scene_duration", source.get("duration", ""))
        ),
    }
    result.update(fields)
    return result

def validate_generated_part(
    generated: Any,
    source_part: dict,
    config: dict,
    total_parts: int,
) -> dict:
    if not isinstance(generated, dict):
        raise ValueError("Model response must be a JSON object.")

    expected_keys = {"scenes", "story_title"}

    for key in ROLE_KEYS:
        if source_part.get(key) is not None:
            expected_keys.add(key)

    unexpected = set(generated) - expected_keys
    missing = expected_keys - set(generated)

    if unexpected:
        raise ValueError(
            "Unexpected generated records/fields: "
            + ", ".join(sorted(unexpected))
        )
    if missing:
        raise ValueError(
            "Missing generated records/fields: "
            + ", ".join(sorted(missing))
        )

    title = clean(generated.get("story_title"))
    if not title:
        raise ValueError("Generated story_title cannot be empty.")

    main_scenes = validate_generated_scenes(
        generated.get("scenes"),
        source_part,
        config,
        total_parts,
    )

    main_narration = " ".join(
        item["narration"] for item in main_scenes
    )
    part_number = source_part["part"]
    final_part = part_number == total_parts

    completed = {
        "part": part_number,
        "status": "completed",
        "title": clean(source_part.get("title")),
        "story_title": title,
        "narration": main_narration,
        "word_count": word_count(main_narration),
        "final_part_merged": bool(source_part.get("final_part_merged")),
        "scenes": main_scenes,
    }

    for key, role in ROLE_KEYS.items():
        source_record = source_part.get(key)

        if key == "suspense_scene" and final_part:
            source_record = None
        if key == "resolution_scene" and not final_part:
            source_record = None

        completed[key] = validate_extra_record(
            generated.get(key),
            source_record,
            role,
            part_number,
        )

    return completed


def build_revision_prompt(
    original_prompt: str,
    generated: dict,
    source_part: dict,
    config: dict,
    total_parts: int,
) -> str:
    scenes = generated.get("scenes", [])
    current_text = " ".join(
        clean(item.get("narration"))
        for item in scenes
        if isinstance(item, dict)
    )
    count = word_count(current_text)
    minimum, maximum = short_word_limits(
        source_part, config, total_parts
    )

    if minimum is None:
        word_rule = "FULL format: preserve the complete story content."
    else:
        word_rule = (
            f"SHORT main narration: {minimum}–{maximum} words. "
            "Count only main narration."
        )
        if source_part["part"] < total_parts:
            word_rule += " Preferred target is 120–150 words."

    return (
        original_prompt
        + "\n\nपिछला उत्तर validation में असफल हुआ।"
        + f"\nमुख्य narration के वर्तमान शब्द: {count}"
        + f"\nसही शब्द सीमा: {word_rule}"
        + "\nइसी भाग का पूरा JSON दोबारा बनाएं।"
        + "\nसभी scene_id, role और scene क्रम source plan के समान रखें।"
        + "\nHook/Suspense/Resolution अलग records में ही रहें।"
        + "\nअतिरिक्त या अनुपस्थित role record न जोड़ें।"
        + "\nवाक्य को बीच में न काटें।"
        + "\nकहानी का अर्थ, घटनाएँ, पात्र और मूल अंत न बदलें।"
        + "\nकेवल JSON लौटाएँ।"
    )


def retryable_http_error(code: int) -> bool:
    return code in {408, 425, 429, 500, 502, 503, 504}


def generate_part(
    api_key: str,
    models: list[str],
    topic: str,
    story_text: str,
    source_part: dict,
    config: dict,
    total_parts: int,
) -> dict:
    original_prompt = build_part_prompt(
        topic,
        story_text,
        source_part,
        config,
        total_parts,
    )
    schema = build_response_schema(source_part)
    errors = []

    for model_index, model in enumerate(models, 1):
        prompt = original_prompt
        model_error = "Unknown generation error."
        max_attempts = config["max_retries"]

        log(
            f"Part {source_part['part']}/{total_parts}: "
            f"trying model {model_index}/{len(models)}: {model}"
        )

        for attempt in range(1, max_attempts + 1):
            try:
                log(
                    f"Part {source_part['part']}; model={model}; "
                    f"attempt {attempt}/{max_attempts}"
                )

                generated = request_gemini(
                    api_key,
                    model,
                    prompt,
                    schema,
                )

                try:
                    completed = validate_generated_part(
                        generated,
                        source_part,
                        config,
                        total_parts,
                    )
                    completed["_katha_model_used"] = model
                    return completed

                except (ValueError, TypeError, KeyError) as exc:
                    model_error = f"Output validation: {exc}"
                    prompt = build_revision_prompt(
                        original_prompt,
                        generated,
                        source_part,
                        config,
                        total_parts,
                    )

            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                model_error = f"HTTP {exc.code}: {body[:1000]}"

                log(f"Model {model} returned HTTP {exc.code}.")

                if not retryable_http_error(exc.code):
                    break

                # Rate limits and temporary overloads move to the
                # next configured model instead of exhausting retries.
                if exc.code in {429, 503, 504}:
                    break

            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
            ) as exc:
                model_error = f"{type(exc).__name__}: {exc}"
                break

            except (
                json.JSONDecodeError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                model_error = f"{type(exc).__name__}: {exc}"

            except Exception as exc:
                model_error = f"{type(exc).__name__}: {exc}"
                break

            log(
                f"Part {source_part['part']} attempt failed "
                f"on {model}: {model_error}"
            )

            if attempt < max_attempts:
                delay = min(30, 3 * (2 ** (attempt - 1)))
                log(f"Retrying same model after {delay} seconds.")
                time.sleep(delay)

        errors.append(f"{model}: {model_error}")
        log(
            f"Model {model} exhausted; "
            "moving to the next configured Gemini model."
        )

    raise RuntimeError(
        f"Part {source_part['part']} failed on all configured Gemini "
        f"models. " + " | ".join(errors)
    )


def source_part_signature(part: dict) -> str:
    return stable_hash({
        "part": part.get("part"),
        "scenes": part.get("scenes"),
        "hook_scene": part.get("hook_scene"),
        "suspense_scene": part.get("suspense_scene"),
        "resolution_scene": part.get("resolution_scene"),
        "final_part_merged": bool(part.get("final_part_merged")),
    })


def build_generation_signature(
    source_data: dict,
    config: dict,
    topic: str,
    story_text: str,
) -> str:
    # Model choice is deliberately excluded. A model outage or fallback
    # change must not invalidate already completed Parts.
    return stable_hash({
        "version": VERSION,
        "source_data": source_data,
        "config": config,
        "topic": topic,
        "story_text": story_text,
    })


def is_completed_part_valid(
    existing: Any,
    source_part: dict,
    config: dict,
    total_parts: int,
) -> bool:
    if not isinstance(existing, dict):
        return False

    if existing.get("status") != "completed":
        return False

    if existing.get("part") != source_part.get("part"):
        return False

    if existing.get("source_part_signature") != (
        source_part_signature(source_part)
    ):
        return False

    if bool(existing.get("final_part_merged")) != bool(
        source_part.get("final_part_merged")
    ):
        return False

    try:
        expected_scenes = source_part["scenes"]
        actual_scenes = existing.get("scenes")

        if not isinstance(actual_scenes, list):
            return False

        if len(actual_scenes) != len(expected_scenes):
            return False

        for source, actual in zip(expected_scenes, actual_scenes):
            if not isinstance(actual, dict):
                return False

            if actual.get("scene") != source.get("scene"):
                return False
            if actual.get("scene_id") != source.get("scene_id"):
                return False
            if actual.get("role") != "main":
                return False
            if actual.get("status") != "completed":
                return False

            for field in REQUIRED_NONEMPTY:
                if not clean(actual.get(field)):
                    return False

        main_words = word_count(
            " ".join(
                clean(item.get("narration"))
                for item in actual_scenes
            )
        )

        minimum, maximum = short_word_limits(
            source_part, config, total_parts
        )

        if minimum is not None and not minimum <= main_words <= maximum:
            return False

        final_part = source_part["part"] == total_parts

        for key, role in ROLE_KEYS.items():
            source = source_part.get(key)

            if key == "suspense_scene" and final_part:
                source = None
            if key == "resolution_scene" and not final_part:
                source = None

            cached = existing.get(key)

            if source is None:
                if cached is not None:
                    return False
                continue

            if not isinstance(cached, dict):
                return False
            if cached.get("scene_id") != source.get("scene_id"):
                return False
            if cached.get("role") != role:
                return False
            if cached.get("status") != "completed":
                return False

            for field in REQUIRED_NONEMPTY:
                if not clean(cached.get(field)):
                    return False

        return bool(clean(existing.get("story_title")))

    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def find_existing_part(parts: list, part_number: int):
    matches = [
        item for item in parts
        if isinstance(item, dict)
        and item.get("part") == part_number
    ]

    if len(matches) > 1:
        raise RuntimeError(
            f"Duplicate cached AI story records for part {part_number}."
        )

    return matches[0] if matches else None


def validate_final_output(
    ai_story: dict,
    source_plan: dict,
    config: dict,
):
    source_parts = source_plan["parts"]
    output_parts = ai_story.get("parts")

    if not isinstance(output_parts, list):
        raise RuntimeError("AI story output parts must be a list.")

    if len(output_parts) != len(source_parts):
        raise RuntimeError(
            f"Final part count mismatch: "
            f"{len(output_parts)}/{len(source_parts)}."
        )

    total_main_scenes = 0
    all_narration = []
    titles = set()

    for index, (source, output) in enumerate(
        zip(source_parts, output_parts), 1
    ):
        if source.get("part") != index or output.get("part") != index:
            raise RuntimeError(f"Part order mismatch at part {index}.")

        if output.get("status") != "completed":
            raise RuntimeError(f"Part {index} is incomplete.")

        if output.get("source_part_signature") != (
            source_part_signature(source)
        ):
            raise RuntimeError(
                f"Part {index} does not match the final Story Plan."
            )

        if bool(output.get("final_part_merged")) != bool(
            source.get("final_part_merged")
        ):
            raise RuntimeError(
                f"Part {index} final_part_merged metadata mismatch."
            )

        expected_scenes = source["scenes"]
        actual_scenes = output.get("scenes")

        if not isinstance(actual_scenes, list):
            raise RuntimeError(f"Part {index} main scenes are invalid.")

        if len(actual_scenes) != len(expected_scenes):
            raise RuntimeError(
                f"Part {index} main scene count mismatch."
            )

        for expected, actual in zip(expected_scenes, actual_scenes):
            if actual.get("scene") != expected.get("scene"):
                raise RuntimeError(
                    f"Part {index} scene order mismatch."
                )
            if actual.get("scene_id") != expected.get("scene_id"):
                raise RuntimeError(
                    f"Part {index} scene_id mismatch."
                )
            if actual.get("role") != "main":
                raise RuntimeError(
                    f"Part {index} main role mismatch."
                )
            if actual.get("status") != "completed":
                raise RuntimeError(
                    f"Part {index} has incomplete scene."
                )

            for field in REQUIRED_NONEMPTY:
                if not clean(actual.get(field)):
                    raise RuntimeError(
                        f"Part {index} scene {expected['scene']}: "
                        f"missing {field}."
                    )

        main_text = " ".join(
            item["narration"] for item in actual_scenes
        )
        main_words = word_count(main_text)

        if output.get("word_count") != main_words:
            raise RuntimeError(
                f"Part {index} word_count metadata mismatch."
            )

        minimum, maximum = short_word_limits(
            source, config, len(source_parts)
        )

        if minimum is not None and not minimum <= main_words <= maximum:
            raise RuntimeError(
                f"SHORT part {index} has {main_words} main words; "
                f"required range is {minimum}–{maximum}."
            )

        if (
            config["format"] == "short"
            and index < len(source_parts)
            and main_words > SHORT_TARGET_MAX
        ):
            raise RuntimeError(
                f"SHORT non-final part {index} exceeds the 150-word target."
            )

        final_part = index == len(source_parts)

        for key, role in ROLE_KEYS.items():
            expected = source.get(key)

            if key == "suspense_scene" and final_part:
                expected = None
            if key == "resolution_scene" and not final_part:
                expected = None

            actual = output.get(key)

            if expected is None:
                if actual is not None:
                    raise RuntimeError(
                        f"Unexpected {role} record in part {index}."
                    )
                continue

            if not isinstance(actual, dict):
                raise RuntimeError(
                    f"Missing separate {role} record in part {index}."
                )

            if actual.get("scene_id") != expected.get("scene_id"):
                raise RuntimeError(
                    f"{role} scene_id mismatch in part {index}."
                )
            if actual.get("role") != role:
                raise RuntimeError(
                    f"{role} role mismatch in part {index}."
                )
            if actual.get("status") != "completed":
                raise RuntimeError(
                    f"{role} record incomplete in part {index}."
                )
            if clean(actual.get("story_anchor")) != clean(
                expected.get("story_anchor")
            ):
                raise RuntimeError(
                    f"{role} story_anchor mismatch in part {index}."
                )

            for field in REQUIRED_NONEMPTY:
                if not clean(actual.get(field)):
                    raise RuntimeError(
                        f"{role} record has empty {field} in part {index}."
                    )

        title = clean(output.get("story_title"))
        if not title:
            raise RuntimeError(f"Part {index} has no story_title.")

        titles.add(re.sub(r"\s+", " ", title).strip().casefold())
        total_main_scenes += len(actual_scenes)
        all_narration.append(main_text)

    if len(titles) != 1:
        raise RuntimeError(
            "Generated story_title differs between parts."
        )

    return len(output_parts), total_main_scenes, all_narration


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    input_config = load_input_config()
    config = build_config()
    models = load_model_order()
    primary_model = models[0]

    source_story = read_json(STORY_FILE)
    source_plan = read_json(PLAN_FILE)

    total_parts, total_scenes = validate_source_plan(
        source_plan,
        config["format"],
    )

    apply_story_part_metadata(source_plan, source_story)

    if len(source_plan["parts"]) != total_parts:
        raise RuntimeError("Story Plan part count changed unexpectedly.")

    topic = (
        clean(get_topic(input_config))
        or clean(source_story.get("topic"))
        or clean(source_story.get("final_title"))
        or clean(source_story.get("title"))
        or clean(source_plan.get("final_title"))
        or "कहानी"
    )

    story_text = (
        clean(get_story_text(input_config))
        or clean(source_story.get("corrected_complete_story"))
        or clean(source_story.get("story_text"))
    )

    if not story_text:
        raise RuntimeError(
            "Final corrected story text is missing from story.json/Input."
        )

    source_story_signature = stable_hash(source_story)
    source_plan_signature = stable_hash(source_plan)

    signature = build_generation_signature(
        {
            "story_signature": source_story_signature,
            "plan_signature": source_plan_signature,
        },
        config,
        topic,
        story_text,
    )

    existing = read_json(OUTPUT_FILE, required=False)

    resumable = (
        isinstance(existing, dict)
        and existing.get("version") == VERSION
        and existing.get("generation_signature") == signature
        and existing.get("source_story_signature") == source_story_signature
        and existing.get("source_plan_signature") == source_plan_signature
        and existing.get("status") in {"in_progress", "completed"}
        and config["resume_enabled"]
    )

    if resumable:
        ai_story = existing
        log("Matching story and plan generation checkpoint found.")
    else:
        # Preserve completed Parts when only the selected Gemini model
        # or fallback list changes.
        reusable_parts = []

        if (
            isinstance(existing, dict)
            and existing.get("source_story_signature") == source_story_signature
            and existing.get("source_plan_signature") == source_plan_signature
            and isinstance(existing.get("parts"), list)
        ):
            reusable_parts = existing["parts"]

        ai_story = {
            "version": VERSION,
            "status": "in_progress",
            "topic": topic,
            "title": clean(source_story.get("title")) or topic,
            "story_text": story_text,
            "format": config["format"],
            "audience": config["audience"],
            "story_length": config["story_length"],
            "scene_duration": config["scene_duration"],
            "model": primary_model,
            "generation_signature": signature,
            "source_story_signature": source_story_signature,
            "source_plan_signature": source_plan_signature,
            "input_config": config,
            "parts": reusable_parts,
        }
        write_json(OUTPUT_FILE, ai_story)

    ai_story.update({
        "version": VERSION,
        "status": "in_progress",
        "topic": topic,
        "story_text": story_text,
        "format": config["format"],
        "audience": config["audience"],
        "story_length": config["story_length"],
        "scene_duration": config["scene_duration"],
        "model": primary_model,
        "model_order": models,
        "generation_signature": signature,
        "source_story_signature": source_story_signature,
        "source_plan_signature": source_plan_signature,
        "input_config": config,
    })

    log("=" * 58)
    log("AI STORY GENERATION")
    log(f"Primary model: {primary_model}")
    log(f"Configured fallback count: {len(models) - 1}")
    log(f"Format: {config['format']}")
    log(f"Parts: {total_parts}")
    log(f"Main scenes: {total_scenes}")
    log("Story source: story.json")
    log("Scene and role source: story_plan.json")
    log("=" * 58)

    cached_parts = ai_story.get("parts", [])

    if not isinstance(cached_parts, list):
        cached_parts = []

    for source_part in source_plan["parts"]:
        part_number = source_part["part"]
        cached = find_existing_part(cached_parts, part_number)

        if (
            config["skip_completed"]
            and is_completed_part_valid(
                cached,
                source_part,
                config,
                total_parts,
            )
        ):
            log(f"Part {part_number}: valid checkpoint reused.")
            continue

        completed = generate_part(
            api_key=api_key,
            models=models,
            topic=topic,
            story_text=story_text,
            source_part=source_part,
            config=config,
            total_parts=total_parts,
        )

        used_model = completed.pop("_katha_model_used", primary_model)
        completed["generated_by_model"] = used_model
        completed["source_part_signature"] = (
            source_part_signature(source_part)
        )

        cached_parts = [
            item for item in cached_parts
            if not (
                isinstance(item, dict)
                and item.get("part") == part_number
            )
        ]
        cached_parts.append(completed)
        cached_parts.sort(key=lambda item: item["part"])

        ai_story["parts"] = cached_parts
        ai_story["status"] = "in_progress"
        ai_story["last_successful_model"] = used_model

        write_json(OUTPUT_FILE, ai_story)
        log(f"Part {part_number}/{total_parts}: checkpoint saved.")

    ai_story["parts"] = cached_parts

    completed_parts, completed_scenes, narrations = validate_final_output(
        ai_story,
        source_plan,
        config,
    )

    if completed_parts != total_parts:
        raise RuntimeError("Final part count does not match Story Plan.")

    if completed_scenes != total_scenes:
        raise RuntimeError("Final main scene count does not match Story Plan.")

    titles = {
        re.sub(r"\s+", " ", clean(item["story_title"])).strip()
        for item in ai_story["parts"]
    }

    if len(titles) != 1:
        raise RuntimeError(
            "Story titles are inconsistent across parts."
        )

    ai_story["title"] = next(iter(titles))
    ai_story["status"] = "completed"
    ai_story["total_parts"] = completed_parts
    ai_story["parts_count"] = completed_parts
    ai_story["total_scenes"] = completed_scenes
    ai_story["total_main_scenes"] = completed_scenes
    ai_story["total_extra_scenes"] = sum(
        1
        for part in ai_story["parts"]
        for key in ROLE_KEYS
        if isinstance(part.get(key), dict)
    )
    ai_story["main_story_text"] = "\n\n".join(narrations)
    ai_story["total_main_story_words"] = word_count(
        ai_story["main_story_text"]
    )
    ai_story["scene_order"] = "part_then_main_scene"
    ai_story["extra_scene_order"] = [
        "hook",
        "main_scenes",
        "suspense_or_resolution",
    ]
    ai_story["completed_at"] = int(time.time())

    write_json(OUTPUT_FILE, ai_story)

    written = read_json(OUTPUT_FILE)

    if written.get("generation_signature") != signature:
        raise RuntimeError(
            "AI story generation signature read-back mismatch."
        )

    validate_final_output(written, source_plan, config)

    log("=" * 58)
    log("AI STORY GENERATION: SUCCESS")
    log(f"Parts: {completed_parts}")
    log(f"Main scenes: {completed_scenes}")
    log(f"Extra role records: {written['total_extra_scenes']}")
    log(f"Last model: {written.get('last_successful_model', primary_model)}")
    log(f"Output: {OUTPUT_FILE}")
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(
            "ERROR: Interrupted. Completed part checkpoints are preserved."
        )
    except Exception as exc:
        raise SystemExit(
            f"ERROR: {type(exc).__name__}: {exc}"
        )
