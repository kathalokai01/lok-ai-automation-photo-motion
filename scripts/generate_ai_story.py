#!/usr/bin/env python3
"""Generate main, hook, suspense and resolution narration records."""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from input_config import (
    load_input_config,
    normalize_format,
    get_max_retries,
    get_topic,
    get_story_text,
)

INPUT = Path("output/story/story.json")
OUTPUT = Path("output/story/ai_story.json")
MODEL_FILE = Path("output/config/selected_model.json")

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
REQUEST_TIMEOUT = 180
MAX_RESPONSE_CHARS = 160000
VERSION = "ai-story-v10-separated-hook-suspense-resolution"

SHORT_MIN_WORDS = 120
SHORT_TARGET_MIN = 120
SHORT_TARGET_MAX = 150
SHORT_HARD_MAX = 180


def log(message=""):
    print(message, flush=True)


def clean(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


def build_config():
    config = load_input_config()
    return {
        "format": normalize_format(config),
        "audience": clean(config.get("AUDIENCE", "adult")) or "adult",
        "story_length": clean(config.get("STORY_LENGTH", "auto")) or "auto",
        "scene_duration": clean(config.get("SCENE_DURATION", "auto")) or "auto",
        "part_hook": cfg_bool(config, "PART_HOOK", True),
        "part_suspense": cfg_bool(config, "PART_SUSPENSE", True),
        "final_resolution": cfg_bool(config, "FINAL_RESOLUTION", True),
        "character_bible": cfg_bool(config, "CHARACTER_BIBLE", True),
        "character_consistency": cfg_bool(config, "CHARACTER_CONSISTENCY", True),
        "world_consistency": cfg_bool(config, "WORLD_CONSISTENCY", True),
        "scene_continuity": cfg_bool(config, "SCENE_CONTINUITY", True),
        "visual_style": clean(config.get("VISUAL_STYLE", "cinematic_realistic")),
        "realism": clean(config.get("REALISM", "high")),
        "cinematic_camera": cfg_bool(config, "CINEMATIC_CAMERA", True),
        "camera_style": clean(config.get("CAMERA_STYLE", "cinematic")),
        "lighting": clean(config.get("LIGHTING", "cinematic")),
        "mood": clean(config.get("MOOD", "dramatic")),
        "quality": clean(config.get("QUALITY", "high")),
        "negative_prompt": cfg_bool(config, "NEGATIVE_PROMPT", True),
        "avoid_cartoon": cfg_bool(config, "AVOID_CARTOON_LOOK", True),
        "avoid_neon": cfg_bool(config, "AVOID_NEON", True),
        "avoid_glitch": cfg_bool(config, "AVOID_GLITCH_EFFECTS", True),
        "natural_motion": cfg_bool(config, "NATURAL_MOTION", True),
        "realistic_lighting": cfg_bool(config, "REALISTIC_LIGHTING", True),
        "music": cfg_bool(config, "MUSIC", True),
        "music_style": clean(config.get("MUSIC_STYLE", "cinematic")),
        "sfx": cfg_bool(config, "SFX", True),
        "ambient_sound": cfg_bool(config, "AMBIENT_SOUND", True),
        "transitions": clean(config.get("TRANSITIONS", "cinematic")),
        "max_retries": max(1, get_max_retries(config)),
        "resume_enabled": cfg_bool(config, "RESUME_ENABLED", True),
        "skip_completed": cfg_bool(config, "SKIP_COMPLETED_SCENES", True),
    }


def read_json(path, required=True):
    path = Path(path)
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"Required file not found: {path}")
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return data


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def stable_hash(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def word_count(text):
    return len(re.findall(r"\S+", clean(text)))


def load_selected_model():
    data = read_json(MODEL_FILE)
    if data.get("status") != "selected":
        raise RuntimeError("Gemini model selection is not in selected state.")
    model = clean(data.get("model")).removeprefix("models/")
    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError("selected_model.json contains an invalid model.")
    return model


def validate_source_plan(story, expected_format):
    if story.get("status") not in {"planned", "completed", "in_progress"}:
        raise RuntimeError(f"Unexpected story plan status: {story.get('status')}")
    if clean(story.get("format")).lower() != expected_format:
        raise RuntimeError("Story plan format does not match Input/topic.txt.")

    parts = story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Story plan contains no parts.")

    total_main_scenes = 0
    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != part_number:
            raise RuntimeError(f"Invalid part numbering at part {part_number}.")
        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"Part {part_number} has no main scenes.")
        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict) or scene.get("scene") != scene_number:
                raise RuntimeError(
                    f"Invalid main scene numbering in part {part_number}."
                )
        total_main_scenes += len(scenes)

        is_final = part_number == len(parts)
        if part.get("hook_scene") is not None:
            validate_extra_source(part["hook_scene"], "hook", part_number)
        if not is_final and part.get("suspense_scene") is not None:
            validate_extra_source(
                part["suspense_scene"], "suspense", part_number
            )
        if is_final and part.get("resolution_scene") is not None:
            validate_extra_source(
                part["resolution_scene"], "resolution", part_number
            )

    if story.get("parts_count") not in (None, len(parts)):
        raise RuntimeError("Story plan parts_count does not match its parts.")
    if story.get("total_scenes") not in (None, total_main_scenes):
        raise RuntimeError("Story plan total_scenes does not match main scenes.")
    return len(parts), total_main_scenes


def validate_extra_source(record, expected_role, part_number):
    if not isinstance(record, dict):
        raise RuntimeError(f"Part {part_number} {expected_role} record is invalid.")
    role = clean(record.get("role")).lower()
    if role and role != expected_role:
        raise RuntimeError(
            f"Part {part_number}: expected {expected_role}, got {role}."
        )
    if not clean(record.get("scene_id")):
        raise RuntimeError(
            f"Part {part_number} {expected_role} is missing scene_id."
        )


def build_response_schema(source_part):
    scene_schema = {
        "type": "object",
        "properties": {
            "scene": {"type": "integer"},
            "narration": {"type": "string"},
            "dialogue": {"type": "string"},
            "visual_prompt": {"type": "string"},
            "negative_prompt": {"type": "string"},
            "camera_prompt": {"type": "string"},
            "lighting_prompt": {"type": "string"},
            "sfx_prompt": {"type": "string"},
            "music_prompt": {"type": "string"},
        },
        "required": [
            "scene", "narration", "dialogue", "visual_prompt",
            "negative_prompt", "camera_prompt", "lighting_prompt",
            "sfx_prompt", "music_prompt",
        ],
    }

    extra_schema = {
        "type": "object",
        "properties": {
            "narration": {"type": "string"},
            "dialogue": {"type": "string"},
            "visual_prompt": {"type": "string"},
            "negative_prompt": {"type": "string"},
            "camera_prompt": {"type": "string"},
            "lighting_prompt": {"type": "string"},
            "sfx_prompt": {"type": "string"},
            "music_prompt": {"type": "string"},
        },
        "required": [
            "narration", "dialogue", "visual_prompt",
            "negative_prompt", "camera_prompt", "lighting_prompt",
            "sfx_prompt", "music_prompt",
        ],
    }

    properties = {
        "scenes": {
            "type": "array",
            "items": scene_schema,
        }
    }
    required = ["scenes"]

    for key in ("hook_scene", "suspense_scene", "resolution_scene"):
        if source_part.get(key) is not None:
            properties[key] = extra_schema
            required.append(key)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def extract_response_text(result):
    candidates = result.get("candidates", [])
    if not candidates:
        raise ValueError(
            f"Gemini returned no candidates: {result.get('promptFeedback', {})}"
        )
    candidate = candidates[0]
    if candidate.get("finishReason") == "SAFETY":
        raise ValueError("Gemini blocked the generated content for safety.")
    parts = candidate.get("content", {}).get("parts", [])
    text_parts = [
        item.get("text", "") for item in parts
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    response = "\n".join(text_parts).strip()
    if not response:
        raise ValueError(
            f"Gemini returned empty content; finishReason="
            f"{candidate.get('finishReason', '')}"
        )
    if len(response) > MAX_RESPONSE_CHARS:
        raise ValueError("Gemini response exceeded the maximum allowed size.")
    return response


def parse_json_response(response_text):
    text = response_text.strip()
    text = re.sub(
        r"^\s*```(?:json)?\s*|\s*```\s*$",
        "", text, flags=re.IGNORECASE
    ).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Gemini response contains no valid JSON object.")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("Gemini response root must be a JSON object.")
    return data


def request_gemini(api_key, model, prompt, schema):
    url = f"{API_BASE}/models/{model}:generateContent"
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}],
        }],
        "generationConfig": {
            "temperature": 0.72,
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
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read().decode("utf-8")
    return parse_json_response(extract_response_text(json.loads(raw)))


def scene_context(part):
    return [
        {
            "scene": item.get("scene"),
            "role": item.get("role", ""),
            "beat": item.get("beat", ""),
            "purpose": item.get("purpose", ""),
            "story_anchor": item.get("story_anchor", ""),
            "visual": item.get("visual", ""),
        }
        for item in part.get("scenes", [])
    ]


def extra_context(part, key):
    record = part.get(key)
    if not isinstance(record, dict):
        return None
    return {
        "scene_id": record.get("scene_id", ""),
        "role": record.get("role", key.removesuffix("_scene")),
        "purpose": record.get("purpose", ""),
        "story_anchor": record.get("story_anchor", ""),
        "visual": record.get("visual", record.get("visual_prompt", "")),
    }


def build_part_prompt(topic, story_text, part, pipeline_config, total_parts):
    number = part["part"]
    is_final = number == total_parts
    main_scenes = scene_context(part)
    hook = extra_context(part, "hook_scene")
    suspense = None if is_final else extra_context(part, "suspense_scene")
    resolution = extra_context(part, "resolution_scene") if is_final else None

    if pipeline_config["format"] == "short":
        format_rules = """
SHORT FORMAT:
- Main narration for this part must contain 120–150 Hindi words.
- Never exceed 180 words. Never return fewer than 120 words.
- Hook and suspense/resolution are separate extra records and are NOT
  included in the 120–150 main narration word count.
- Do not insert hook or suspense text into main scene narration.
"""
    else:
        format_rules = """
FULL FORMAT:
- Preserve the full story's events, emotional development and pacing.
- Do not compress the story into a summary.
- Hook and suspense/resolution remain separate extra records.
"""

    prompt = f"""
आप हिंदी कहानी और सिनेमैटिक दृश्य-लेखन इंजन हैं।

विषय: {topic}
पूरी मूल कहानी:
{story_text or "[कोई पूरी कहानी नहीं दी गई; विषय के आधार पर कहानी लिखें।]"}

भाग: {number}/{total_parts}
फॉर्मेट: {pipeline_config['format']}
दर्शक: {pipeline_config['audience']}
कहानी अवधि: {pipeline_config['story_length']}
दृश्य अवधि सेटिंग: {pipeline_config['scene_duration']}

इस भाग के मुख्य दृश्यों का निश्चित क्रम:
{json.dumps(main_scenes, ensure_ascii=False, indent=2)}

अलग Hook रिकॉर्ड:
{json.dumps(hook, ensure_ascii=False, indent=2)}

अलग Suspense रिकॉर्ड:
{json.dumps(suspense, ensure_ascii=False, indent=2)}

अलग Final Resolution रिकॉर्ड:
{json.dumps(resolution, ensure_ascii=False, indent=2)}

{format_rules}

कहानी के अनिवार्य नियम:
- STORY_TEXT उपलब्ध हो तो वही प्राथमिक और प्रामाणिक स्रोत है।
- मूल घटनाएँ, पात्र, संबंध, स्थान, कारण-परिणाम और मूल अंत न बदलें।
- मूल कहानी के बाहर नया मुख्य पात्र या असंबंधित घटना न जोड़ें।
- हिंदी की वर्तनी, मात्रा, व्याकरण और विराम-चिह्न सही रखें।
- हर मुख्य दृश्य की narration उसी दृश्य के story_anchor और उद्देश्य से मेल खाए।
- मुख्य दृश्यों का क्रम और scene number बिल्कुल न बदलें।
- हर दृश्य की narration बोलने योग्य, स्वाभाविक और भावपूर्ण हो।
- संवाद केवल तभी लिखें जब पात्र वास्तव में बोल रहा हो।
- एक ही जानकारी को बार-बार न दोहराएँ।
- सभी भागों में पात्र, संबंध, समय, स्थान और घटनाओं की निरंतरता रखें।

HOOK नियम:
- Hook अलग रिकॉर्ड में हो; मुख्य scenes में न मिलाएँ।
- पहले ही वाक्य में कहानी-विशिष्ट रहस्य, खतरा, असामान्य घटना या जिज्ञासा हो।
- सामान्य/घिसा-पिटा वाक्य जैसे 'एक समय की बात है' न लिखें।
- Hook आगे की कहानी से जुड़ा हो; झूठा clickbait न हो।
- Hook में मुख्य कहानी का समाधान उजागर न करें।

SUSPENSE नियम:
- केवल non-final भाग में suspense रिकॉर्ड दें, यदि source plan में मौजूद है।
- Suspense उसी भाग की वास्तविक घटना और अगले भाग के अनसुलझे प्रश्न से जुड़ा हो।
- ऐसा सवाल/खतरा छोड़ें जिसका उत्तर आगे की कहानी में मिलता हो।
- असंबंधित रहस्य या मनगढ़ंत cliffhanger न बनाएँ।
- Final part में suspense record न दें।

FINAL RESOLUTION नियम:
- केवल अंतिम भाग में resolution रिकॉर्ड दें, यदि source plan में मौजूद है।
- मूल कहानी के वास्तविक संघर्ष और अंत का समाधान दें।
- नया अंत, नया खलनायक या अनसुलझा नया संघर्ष न जोड़ें।
- अंतिम भाग का अंत पूर्ण और संतोषजनक हो।

दृश्य और विजुअल:
- हर visual_prompt उसी narration में वर्णित सटीक क्षण दिखाए।
- पात्रों का चेहरा, आयु, लिंग, शरीर, बाल और कपड़े निरंतर समान रखें।
- सामान्य मानव शरीर रचना रखें; अतिरिक्त/गायब अंग न हों।
- वास्तविक live-action cinematic look रखें: {pipeline_config['visual_style']}
- Realism: {pipeline_config['realism']}
- Camera: {pipeline_config['camera_style']}
- Lighting: {pipeline_config['lighting']}
- Mood: {pipeline_config['mood']}
- Quality: {pipeline_config['quality']}
- Cartoon/anime/comic look से बचें: {pipeline_config['avoid_cartoon']}
- Neon से बचें: {pipeline_config['avoid_neon']}
- Glitch से बचें: {pipeline_config['avoid_glitch']}
- प्राकृतिक गति: {pipeline_config['natural_motion']}
- यथार्थवादी प्रकाश: {pipeline_config['realistic_lighting']}

Audio निर्देश:
- Music enabled: {pipeline_config['music']}
- Music style: {pipeline_config['music_style']}
- SFX enabled: {pipeline_config['sfx']}
- Ambient enabled: {pipeline_config['ambient_sound']}
- केवल दृश्य के अनुरूप sound prompts दें; अनावश्यक hiss, whistle या तीखी आवाज़ न सुझाएँ।

OUTPUT:
- केवल दिए गए JSON schema के अनुसार JSON लौटाएँ।
- scenes में ठीक उतने ही मुख्य दृश्य लौटाएँ जितने माँगे गए हैं।
- Hook/Suspense/Resolution केवल अपने अलग JSON keys में लौटाएँ।
- हर main scene में scene number और सभी schema fields अनिवार्य हैं।
- हर extra record में narration, dialogue, visual_prompt, negative_prompt,
  camera_prompt, lighting_prompt, sfx_prompt और music_prompt अनिवार्य हैं।
- Markdown, टिप्पणी या JSON के बाहर कोई text न दें।
"""
    return prompt

def validate_extra_record(record, source_record, role):
    if source_record is None:
        if record is not None:
            raise ValueError(f"Unexpected {role} record was generated.")
        return None

    if not isinstance(record, dict):
        raise ValueError(f"Required separate {role} record is missing.")

    required = (
        "narration",
        "dialogue",
        "visual_prompt",
        "negative_prompt",
        "camera_prompt",
        "lighting_prompt",
        "sfx_prompt",
        "music_prompt",
    )
    result = {
        "scene_id": clean(source_record.get("scene_id")),
        "role": role,
        "status": "completed",
    }

    for field in required:
        value = record.get(field)
        if not isinstance(value, str):
            raise ValueError(f"{role}.{field} must be text.")
        value = value.strip()
        if field in {
            "narration", "visual_prompt", "camera_prompt", "lighting_prompt"
        } and not value:
            raise ValueError(f"{role}.{field} cannot be empty.")
        result[field] = value

    result["purpose"] = clean(source_record.get("purpose"))
    result["story_anchor"] = clean(source_record.get("story_anchor"))
    result["scene_duration"] = clean(
        source_record.get("scene_duration", source_record.get("duration", ""))
    )
    return result


def validate_generated_scenes(generated, source_part, pipeline_config):
    if not isinstance(generated, list):
        raise ValueError("Generated main scenes must be a list.")

    expected = source_part.get("scenes", [])
    if len(generated) != len(expected):
        raise ValueError(
            f"Expected {len(expected)} main scenes, received {len(generated)}."
        )

    expected_numbers = [item["scene"] for item in expected]
    actual_numbers = [item.get("scene") for item in generated]
    if actual_numbers != expected_numbers:
        raise ValueError(
            f"Main scene numbering mismatch: expected {expected_numbers}, "
            f"received {actual_numbers}."
        )

    required = (
        "narration",
        "dialogue",
        "visual_prompt",
        "negative_prompt",
        "camera_prompt",
        "lighting_prompt",
        "sfx_prompt",
        "music_prompt",
    )
    clean_scenes = []

    for source, item in zip(expected, generated):
        row = {
            "scene": source["scene"],
            "role": clean(source.get("role")) or "main",
            "status": "completed",
            "scene_id": clean(source.get("scene_id")),
            "story_anchor": clean(source.get("story_anchor")),
            "purpose": clean(source.get("purpose")),
            "beat": clean(source.get("beat")),
            "scene_duration": clean(
                source.get("scene_duration", source.get("duration", ""))
            ),
        }

        for field in required:
            value = item.get(field)
            if not isinstance(value, str):
                raise ValueError(
                    f"Scene {source['scene']} field {field} is not text."
                )
            value = value.strip()
            if field in {
                "narration", "visual_prompt", "camera_prompt", "lighting_prompt"
            } and not value:
                raise ValueError(
                    f"Scene {source['scene']} has empty {field}."
                )
            row[field] = value

        clean_scenes.append(row)

    count = word_count(" ".join(x["narration"] for x in clean_scenes))
    if pipeline_config["format"] == "short":
        if not SHORT_MIN_WORDS <= count <= SHORT_HARD_MAX:
            raise ValueError(
                f"SHORT main narration has {count} words; allowed range "
                f"is {SHORT_MIN_WORDS}–{SHORT_HARD_MAX}."
            )
        if count > SHORT_TARGET_MAX:
            raise ValueError(
                f"SHORT main narration has {count} words; it must be revised "
                f"to {SHORT_TARGET_MIN}–{SHORT_TARGET_MAX} words."
            )

    return clean_scenes


def validate_generated_part(result, source_part, pipeline_config):
    if not isinstance(result, dict):
        raise ValueError("Gemini part response must be a JSON object.")

    main_scenes = validate_generated_scenes(
        result.get("scenes"),
        source_part,
        pipeline_config,
    )

    completed = {
        "part": source_part["part"],
        "status": "completed",
        "title": clean(source_part.get("title")),
        "narration": " ".join(x["narration"] for x in main_scenes),
        "word_count": word_count(
            " ".join(x["narration"] for x in main_scenes)
        ),
        "scenes": main_scenes,
    }

    for key, role in (
        ("hook_scene", "hook"),
        ("suspense_scene", "suspense"),
        ("resolution_scene", "resolution"),
    ):
        source_record = source_part.get(key)
        generated_record = result.get(key)

        if source_record is None:
            if generated_record is not None:
                raise ValueError(
                    f"Unexpected {role} output for part {source_part['part']}."
                )
            completed[key] = None
        else:
            completed[key] = validate_extra_record(
                generated_record,
                source_record,
                role,
            )

    return completed


def generate_part(
    api_key,
    model,
    topic,
    story_text,
    source_part,
    pipeline_config,
    total_parts,
):
    prompt = build_part_prompt(
        topic,
        story_text,
        source_part,
        pipeline_config,
        total_parts,
    )
    schema = build_response_schema(source_part)
    retries = pipeline_config["max_retries"]
    last_error = "Unknown generation error."

    for attempt in range(1, retries + 1):
        try:
            log(
                f"Generating part {source_part['part']}/{total_parts}; "
                f"attempt {attempt}/{retries}; model={model}"
            )
            result = request_gemini(api_key, model, prompt, schema)
            return validate_generated_part(
                result, source_part, pipeline_config
            )

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {body[:1200]}"
            log(last_error)
            if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                break

        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            last_error = str(exc)
            log(f"Part {source_part['part']} failed: {last_error}")

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log(f"Part {source_part['part']} failed: {last_error}")

        if attempt < retries:
            delay = min(30, 3 * (2 ** (attempt - 1)))
            log(f"Retrying after {delay} seconds.")
            time.sleep(delay)

    raise RuntimeError(
        f"Part {source_part['part']} failed after {retries} attempts: "
        f"{last_error}"
    )


def build_generation_signature(story, pipeline_config, topic, story_text, model):
    return stable_hash({
        "version": VERSION,
        "source_plan": story,
        "pipeline_config": pipeline_config,
        "topic": topic,
        "story_text": story_text,
        "model": model,
    })


def source_record_signature(part):
    return stable_hash({
        "part": part.get("part"),
        "scenes": part.get("scenes"),
        "hook_scene": part.get("hook_scene"),
        "suspense_scene": part.get("suspense_scene"),
        "resolution_scene": part.get("resolution_scene"),
    })


def is_completed_part_valid(existing, source_part, pipeline_config):
    if not isinstance(existing, dict):
        return False
    if existing.get("status") != "completed":
        return False
    if existing.get("part") != source_part.get("part"):
        return False
    if existing.get("source_part_signature") != source_record_signature(
        source_part
    ):
        return False

    scenes = existing.get("scenes")
    expected = source_part.get("scenes", [])
    if not isinstance(scenes, list) or len(scenes) != len(expected):
        return False

    try:
        narration_words = 0
        for source, scene in zip(expected, scenes):
            if scene.get("scene") != source.get("scene"):
                return False
            if scene.get("status") != "completed":
                return False
            for field in (
                "narration", "visual_prompt", "camera_prompt", "lighting_prompt"
            ):
                if not clean(scene.get(field)):
                    return False
            narration_words += word_count(scene.get("narration", ""))

        if pipeline_config["format"] == "short":
            if not SHORT_MIN_WORDS <= narration_words <= SHORT_TARGET_MAX:
                return False

        for key in ("hook_scene", "suspense_scene", "resolution_scene"):
            source_record = source_part.get(key)
            cached_record = existing.get(key)
            if source_record is None:
                if cached_record is not None:
                    return False
            else:
                if not isinstance(cached_record, dict):
                    return False
                if cached_record.get("status") != "completed":
                    return False
                if cached_record.get("scene_id") != source_record.get("scene_id"):
                    return False
                for field in (
                    "narration", "visual_prompt", "camera_prompt", "lighting_prompt"
                ):
                    if not clean(cached_record.get(field)):
                        return False

        return True
    except (TypeError, ValueError, KeyError):
        return False


def find_existing_part(parts, part_number):
    for item in parts:
        if isinstance(item, dict) and item.get("part") == part_number:
            return item
    return None


def validate_final_output(ai_story, source_story, pipeline_config):
    source_parts = source_story.get("parts", [])
    output_parts = ai_story.get("parts", [])

    if len(output_parts) != len(source_parts):
        raise RuntimeError(
            f"Final part count mismatch: {len(output_parts)}/{len(source_parts)}"
        )

    total_main_scenes = 0
    all_main_narration = []

    for index, (source, output) in enumerate(
        zip(source_parts, output_parts), 1
    ):
        if output.get("part") != index or source.get("part") != index:
            raise RuntimeError(f"Incorrect part order at part {index}.")
        if output.get("status") != "completed":
            raise RuntimeError(f"Part {index} is incomplete.")

        expected_scenes = source.get("scenes", [])
        actual_scenes = output.get("scenes", [])
        if len(actual_scenes) != len(expected_scenes):
            raise RuntimeError(f"Main scene count mismatch in part {index}.")

        for scene_number, (expected, actual) in enumerate(
            zip(expected_scenes, actual_scenes), 1
        ):
            if actual.get("scene") != scene_number:
                raise RuntimeError(
                    f"Main scene order mismatch in part {index}."
                )
            if actual.get("status") != "completed":
                raise RuntimeError(
                    f"Part {index}, scene {scene_number} is incomplete."
                )
            for field in (
                "narration", "visual_prompt", "camera_prompt", "lighting_prompt"
            ):
                if not clean(actual.get(field)):
                    raise RuntimeError(
                        f"Missing {field} in part {index}, scene {scene_number}."
                    )

        main_text = " ".join(x["narration"] for x in actual_scenes)
        main_words = word_count(main_text)
        if output.get("word_count") != main_words:
            raise RuntimeError(f"Part {index} word count metadata is incorrect.")

        if pipeline_config["format"] == "short":
            if not SHORT_MIN_WORDS <= main_words <= SHORT_TARGET_MAX:
                raise RuntimeError(
                    f"SHORT part {index} has {main_words} words; "
                    "required range is 120–150."
                )

        is_final = index == len(source_parts)
        expected_extra_keys = {
            "hook_scene": source.get("hook_scene"),
            "suspense_scene": (
                None if is_final else source.get("suspense_scene")
            ),
            "resolution_scene": (
                source.get("resolution_scene") if is_final else None
            ),
        }

        for key, expected in expected_extra_keys.items():
            actual = output.get(key)
            if expected is None:
                if actual is not None:
                    raise RuntimeError(
                        f"Unexpected {key} record in part {index}."
                    )
                continue

            if not isinstance(actual, dict):
                raise RuntimeError(
                    f"Required separate {key} missing in part {index}."
                )
            if actual.get("scene_id") != expected.get("scene_id"):
                raise RuntimeError(
                    f"{key} scene_id mismatch in part {index}."
                )
            if actual.get("status") != "completed":
                raise RuntimeError(f"{key} is incomplete in part {index}.")
            for field in (
                "narration", "visual_prompt", "camera_prompt", "lighting_prompt"
            ):
                if not clean(actual.get(field)):
                    raise RuntimeError(
                        f"{key} has empty {field} in part {index}."
                    )

        total_main_scenes += len(actual_scenes)
        all_main_narration.append(main_text)

    flattened = [
        scene
        for part in output_parts
        for scene in part.get("scenes", [])
    ]
    if len(flattened) != total_main_scenes:
        raise RuntimeError("Top-level main scene total is inconsistent.")

    return len(output_parts), total_main_scenes, all_main_narration


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    input_config = load_input_config()
    pipeline_config = build_config()
    model = load_selected_model()
    story = read_json(INPUT)

    total_parts, total_scenes = validate_source_plan(
        story, pipeline_config["format"]
    )

    topic = clean(get_topic(input_config)) or clean(story.get("topic"))
    story_text = clean(get_story_text(input_config)) or clean(
        story.get("story_text")
    )
    if not topic:
        topic = clean(story.get("title")) or "STORY_TEXT से बनी कहानी"

    signature = build_generation_signature(
        story, pipeline_config, topic, story_text, model
    )

    existing = read_json(OUTPUT, required=False)
    resumable = (
        isinstance(existing, dict)
        and existing.get("generation_signature") == signature
        and existing.get("status") in {"in_progress", "completed"}
        and pipeline_config["resume_enabled"]
    )

    if resumable:
        log("Matching AI story checkpoint found; completed parts will be reused.")
        ai_story = existing
    else:
        ai_story = {
            "version": VERSION,
            "status": "in_progress",
            "topic": topic,
            "title": clean(story.get("title")) or topic,
            "story_text": story_text,
            "format": pipeline_config["format"],
            "audience": pipeline_config["audience"],
            "story_length": pipeline_config["story_length"],
            "scene_duration": pipeline_config["scene_duration"],
            "model": model,
            "generation_signature": signature,
            "input_config": pipeline_config,
            "parts": [],
        }
        write_json(OUTPUT, ai_story)

    # Current source and settings remain authoritative when resuming.
    ai_story.update({
        "version": VERSION,
        "status": "in_progress",
        "topic": topic,
        "title": clean(story.get("title")) or topic,
        "story_text": story_text,
        "format": pipeline_config["format"],
        "audience": pipeline_config["audience"],
        "story_length": pipeline_config["story_length"],
        "scene_duration": pipeline_config["scene_duration"],
        "model": model,
        "generation_signature": signature,
        "input_config": pipeline_config,
    })

    log("=" * 58)
    log("AI STORY GENERATION")
    log(f"Model: {model}")
    log(f"Format: {pipeline_config['format']}")
    log(f"Parts: {total_parts}")
    log(f"Main scenes: {total_scenes}")
    log("Hook/Suspense/Resolution: separate records")
    log("=" * 58)

    for source_part in story["parts"]:
        part_number = source_part["part"]
        cached = find_existing_part(ai_story.get("parts", []), part_number)

        if (
            pipeline_config["skip_completed"]
            and is_completed_part_valid(cached, source_part, pipeline_config)
        ):
            log(f"Part {part_number}: valid completed checkpoint reused.")
            continue

        completed_part = generate_part(
            api_key=api_key,
            model=model,
            topic=topic,
            story_text=story_text,
            source_part=source_part,
            pipeline_config=pipeline_config,
            total_parts=total_parts,
        )
        completed_part["source_part_signature"] = source_record_signature(
            source_part
        )

        current_parts = [
            item for item in ai_story.get("parts", [])
            if isinstance(item, dict) and item.get("part") != part_number
        ]
        current_parts.append(completed_part)
        current_parts.sort(key=lambda item: item["part"])
        ai_story["parts"] = current_parts
        ai_story["status"] = "in_progress"

        # Persist immediately after every successful part.
        write_json(OUTPUT, ai_story)
        log(f"Part {part_number}/{total_parts}: checkpoint saved.")

    completed_parts, completed_scenes, part_narrations = validate_final_output(
        ai_story, story, pipeline_config
    )

    if completed_parts != total_parts or completed_scenes != total_scenes:
        raise RuntimeError("Final AI story part/scene totals do not match plan.")

    ai_story["status"] = "completed"
    ai_story["total_parts"] = completed_parts
    ai_story["parts_count"] = completed_parts
    ai_story["total_scenes"] = completed_scenes
    ai_story["main_story_text"] = "\n\n".join(part_narrations)
    ai_story["total_main_story_words"] = word_count(
        ai_story["main_story_text"]
    )
    ai_story["scene_order"] = "part_then_main_scene"
    ai_story["extra_scene_order"] = [
        "hook", "main_scenes", "suspense_or_resolution"
    ]
    write_json(OUTPUT, ai_story)

    log("=" * 58)
    log("AI STORY GENERATION: SUCCESS")
    log(f"Parts: {completed_parts}")
    log(f"Main scenes: {completed_scenes}")
    log(f"Model: {model}")
    log(f"Output: {OUTPUT}")
    log("=" * 58)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(
            "ERROR: Interrupted. Completed part checkpoints are preserved."
        )
    except Exception as exc:
        raise SystemExit(f"ERROR: {type(exc).__name__}: {exc}")