#!/usr/bin/env python3
"""Generate and validate story narration, visual prompts and role records."""

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
VERSION = "ai-story-v11-proofread-role-safe"

SHORT_MIN_WORDS = 120
SHORT_TARGET_MIN = 120
SHORT_TARGET_MAX = 150
SHORT_HARD_MAX = 180

ROLE_ORDER = {
    "hook": 0,
    "main": 1,
    "suspense": 2,
    "resolution": 3,
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


def log(message=""):
    print(message, flush=True)


def clean(value):
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in {"true", "1", "yes", "y", "on"}:
        return True
    if value in {"false", "0", "no", "n", "off"}:
        return False
    return default


def word_count(value):
    return len(re.findall(r"\S+", clean(value)))


def stable_hash(data):
    raw = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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


def load_selected_model():
    data = read_json(MODEL_FILE)
    if data.get("status") != "selected":
        raise RuntimeError("Selected model file is not in selected state.")

    model = clean(data.get("model")).removeprefix("models/")
    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError("Selected model name is invalid.")

    return model


def validate_extra_source(record, expected_role, part_number):
    if not isinstance(record, dict):
        raise RuntimeError(
            f"Part {part_number} {expected_role} record is invalid."
        )

    role = clean(record.get("role")).lower()
    if role and role != expected_role:
        raise RuntimeError(
            f"Part {part_number}: expected {expected_role}, got {role}."
        )

    if not clean(record.get("scene_id")):
        raise RuntimeError(
            f"Part {part_number} {expected_role} has no scene_id."
        )

    if not clean(record.get("story_anchor")):
        raise RuntimeError(
            f"Part {part_number} {expected_role} has no story anchor."
        )


def validate_source_plan(story, expected_format):
    if story.get("status") not in {
        "planned", "completed", "in_progress"
    }:
        raise RuntimeError(
            f"Unexpected story plan status: {story.get('status')}"
        )

    if clean(story.get("format")).lower() != expected_format:
        raise RuntimeError(
            "Story plan format does not match Input/topic.txt."
        )

    parts = story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Story plan contains no parts.")

    if story.get("parts_count") not in (None, len(parts)):
        raise RuntimeError("Story plan parts_count mismatch.")

    total_main_scenes = 0

    for part_number, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid part record: {part_number}")

        if part.get("part") != part_number:
            raise RuntimeError(f"Invalid part numbering: {part_number}")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"Part {part_number} has no main scenes.")

        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict):
                raise RuntimeError(
                    f"Invalid scene in part {part_number}."
                )

            if scene.get("scene") != scene_number:
                raise RuntimeError(
                    f"Invalid scene numbering in part {part_number}."
                )

            if not clean(scene.get("scene_id")):
                raise RuntimeError(
                    f"Part {part_number} scene {scene_number} "
                    "has no scene_id."
                )

            if not clean(scene.get("story_anchor")):
                raise RuntimeError(
                    f"Part {part_number} scene {scene_number} "
                    "has no story anchor."
                )

        total_main_scenes += len(scenes)

        final_part = part_number == len(parts)

        if part.get("hook_scene") is not None:
            validate_extra_source(
                part["hook_scene"], "hook", part_number
            )

        if not final_part and part.get("suspense_scene") is not None:
            validate_extra_source(
                part["suspense_scene"], "suspense", part_number
            )

        if final_part and part.get("resolution_scene") is not None:
            validate_extra_source(
                part["resolution_scene"], "resolution", part_number
            )

        if final_part and part.get("suspense_scene") is not None:
            raise RuntimeError(
                "Final part must not contain a suspense record."
            )

        if not final_part and part.get("resolution_scene") is not None:
            raise RuntimeError(
                "Resolution is allowed only in the final part."
            )

    if story.get("total_scenes") not in (None, total_main_scenes):
        raise RuntimeError("Story plan total_scenes mismatch.")

    return len(parts), total_main_scenes


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
        "required": ["scene", *REQUIRED_FIELDS],
    }

    extra_schema = {
        "type": "object",
        "properties": {
            field: {"type": "string"}
            for field in REQUIRED_FIELDS
        },
        "required": list(REQUIRED_FIELDS),
    }

    properties = {
        "scenes": {
            "type": "array",
            "items": scene_schema,
        }
    }

    required = ["scenes"]

    for key in (
        "hook_scene",
        "suspense_scene",
        "resolution_scene",
    ):
        if source_part.get(key) is not None:
            properties[key] = extra_schema
            required.append(key)

    properties["story_title"] = {"type": "string"}
    required.append("story_title")

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def extract_response_text(result):
    candidates = result.get("candidates", [])
    if not candidates:
        raise ValueError(
            "Gemini returned no candidates: "
            + str(result.get("promptFeedback", {}))
        )

    candidate = candidates[0]
    if candidate.get("finishReason") == "SAFETY":
        raise ValueError("Gemini blocked the generated content.")

    parts = candidate.get("content", {}).get("parts", [])
    response = "\n".join(
        item.get("text", "")
        for item in parts
        if isinstance(item, dict)
        and isinstance(item.get("text"), str)
    ).strip()

    if not response:
        raise ValueError("Gemini returned empty content.")

    if len(response) > MAX_RESPONSE_CHARS:
        raise ValueError("Gemini response exceeded maximum size.")

    return response


def parse_json_response(response_text):
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
            raise ValueError("Response contains no JSON object.")
        result = json.loads(value[start:end + 1])

    if not isinstance(result, dict):
        raise ValueError("Response JSON root must be an object.")

    return result


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
        data=json.dumps(
            payload, ensure_ascii=False
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    with urllib.request.urlopen(
        request, timeout=REQUEST_TIMEOUT
    ) as response:
        raw = response.read().decode("utf-8")

    return parse_json_response(
        extract_response_text(json.loads(raw))
    )


def scene_context(part):
    return [
        {
            "scene": item.get("scene"),
            "scene_id": item.get("scene_id"),
            "role": item.get("role", "main"),
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
        "role": record.get(
            "role", key.removesuffix("_scene")
        ),
        "purpose": record.get("purpose", ""),
        "story_anchor": record.get("story_anchor", ""),
        "visual": record.get(
            "visual", record.get("visual_prompt", "")
        ),
    }


def build_part_prompt(
    topic,
    story_text,
    part,
    pipeline_config,
    total_parts,
):
    number = part["part"]
    is_final = number == total_parts

    if pipeline_config["format"] == "short":
        format_rules = """
SHORT FORMAT:
- Main narration must contain 120–150 Hindi words per part.
- 151–180 words require revision before acceptance.
- Fewer than 120 or more than 180 words must fail validation.
- Count only main narration, excluding hook/suspense/resolution.
- Do not combine role narration with main narration.
"""
    else:
        format_rules = """
FULL FORMAT:
- Preserve the complete story and all its important events.
- Do not turn the story into a summary.
- Do not remove important emotional developments.
- Do not change the original ending.
"""

    main_scenes = scene_context(part)
    hook = extra_context(part, "hook_scene")
    suspense = (
        None if is_final
        else extra_context(part, "suspense_scene")
    )
    resolution = (
        extra_context(part, "resolution_scene")
        if is_final else None
    )

    return f"""
आप हिंदी कहानी-संपादन, प्रूफरीडिंग और सिनेमैटिक नैरेशन इंजन हैं।

विषय: {topic}

पूरी मूल कहानी:
{story_text or "[विषय के आधार पर तैयार कहानी]"}

भाग: {number}/{total_parts}
फॉर्मेट: {pipeline_config['format']}
दर्शक: {pipeline_config['audience']}
कहानी अवधि: {pipeline_config['story_length']}
दृश्य अवधि: {pipeline_config['scene_duration']}

इस भाग के मुख्य दृश्यों का निश्चित क्रम:
{json.dumps(main_scenes, ensure_ascii=False, indent=2)}

अलग Hook:
{json.dumps(hook, ensure_ascii=False, indent=2)}

अलग Suspense:
{json.dumps(suspense, ensure_ascii=False, indent=2)}

अलग Final Resolution:
{json.dumps(resolution, ensure_ascii=False, indent=2)}

{format_rules}

कहानी संपादन और प्रूफरीडिंग:
- पूरी मूल कहानी को संदर्भ मानकर घटनाओं का क्रम सुरक्षित रखें।
- मूल पात्र, नाम, संबंध, स्थान, कारण और परिणाम न बदलें।
- कहानी का अर्थ, संदेश और मूल अंत न बदलें।
- हिंदी वर्तनी, मात्रा, व्याकरण, लिंग, वचन और काल ठीक करें।
- गलत या दोहराए गए वाक्य सुधारें।
- अधूरे वाक्य, अस्पष्ट सर्वनाम और अस्वाभाविक भाषा सुधारें।
- कहानी में अनावश्यक दोहराव और भराव हटाएँ।
- कोई नई मुख्य घटना, पात्र या असंबंधित मोड़ न जोड़ें।
- हर दृश्य की narration उसके story_anchor से मेल खाए।
- दृश्यों का क्रम और scene number बिल्कुल न बदलें।
- मुख्य narration में Hook या Suspense का पाठ न मिलाएँ।
- संवाद स्वाभाविक और पात्र के अनुरूप हों।
- हर भाग की narration पिछले और अगले भाग से जुड़ी रहे।

भाग-विभाजन:
- मुख्य narration को केवल स्वाभाविक वाक्य सीमाओं पर रखें।
- पूर्ण विराम, प्रश्नवाचक, विस्मयादिबोधक और अर्थपूर्ण विराम को प्राथमिकता दें।
- आवश्यकता पर अल्पविराम, अर्धविराम या पूर्ण भाव वाले clause पर विभाजन करें।
- किसी वाक्य को बीच में काटकर scene में न बाँटें।
- वाक्य का अर्थ या भाव अधूरा छोड़कर अगला scene शुरू न करें।
- कहानी की घटनाओं का क्रम सुरक्षित रखें।

HOOK:
- अलग रिकॉर्ड में कहानी-विशिष्ट Hook लिखें।
- पहले वाक्य में वास्तविक रहस्य, खतरा या जिज्ञासा हो।
- घिसा-पिटा आरंभ और झूठा clickbait न हो।
- मूल कहानी से बाहर की घटना न गढ़ें।
- मुख्य समाधान पहले से उजागर न करें।

SUSPENSE:
- केवल non-final भाग में, यदि source plan में मौजूद हो।
- वास्तविक घटना और अगले भाग के प्रश्न से जुड़ा हो।
- मनगढ़ंत खतरा या असंबंधित cliffhanger न बनाएँ।
- अंतिम भाग में suspense न दें।

FINAL RESOLUTION:
- केवल अंतिम भाग में, यदि source plan में मौजूद हो।
- मूल संघर्ष और कहानी का वास्तविक समाधान दें।
- नया अंत या नया अनसुलझा संघर्ष न जोड़ें।

कहानी का शीर्षक:
- पूरी कहानी के सार, मुख्य संघर्ष और परिणाम को समझकर शीर्षक बनाएं।
- केवल शुरुआती पंक्ति या पहले दृश्य से शीर्षक न बनाएं।
- शीर्षक संक्षिप्त, विशिष्ट और कहानी के अर्थ के अनुरूप हो।
- शीर्षक में ऐसा पात्र या घटना न जोड़ें जो कहानी में नहीं है।
- सभी भागों के उत्तर में एक ही कहानी के लिए समान शीर्षक दें।

VISUAL:
- हर visual_prompt narration के वास्तविक क्षण को दिखाए।
- पात्रों का चेहरा, आयु, शरीर, बाल और कपड़े निरंतर समान रहें।
- वास्तविक live-action cinematic look रखें।
- Cartoon, anime, comic या slideshow शैली न दें।
- मानव शरीर रचना स्वाभाविक रखें।
- अनावश्यक text, watermark, logo या subtitle न दिखाएँ।
- दृश्य में असंबंधित पात्र या वस्तु न जोड़ें।
- Visual style: {pipeline_config['visual_style']}
- Realism: {pipeline_config['realism']}
- Camera: {pipeline_config['camera_style']}
- Lighting: {pipeline_config['lighting']}
- Mood: {pipeline_config['mood']}
- Quality: {pipeline_config['quality']}
- Avoid cartoon: {pipeline_config['avoid_cartoon']}
- Avoid neon: {pipeline_config['avoid_neon']}
- Avoid glitch: {pipeline_config['avoid_glitch']}
- Natural motion: {pipeline_config['natural_motion']}
- Realistic lighting: {pipeline_config['realistic_lighting']}

AUDIO:
- Music enabled: {pipeline_config['music']}
- Music style: {pipeline_config['music_style']}
- SFX enabled: {pipeline_config['sfx']}
- Ambient enabled: {pipeline_config['ambient_sound']}
- Sound prompts दृश्य और narration से मेल खाने चाहिए।
- अनावश्यक hiss, whistle या तीखी आवाज़ न सुझाएँ।

प्रत्येक मुख्य scene में ये सभी fields दें:
scene, narration, dialogue, visual_prompt, negative_prompt,
camera_prompt, lighting_prompt, sfx_prompt, music_prompt.

प्रत्येक उपलब्ध extra record में ये सभी fields दें:
narration, dialogue, visual_prompt, negative_prompt,
camera_prompt, lighting_prompt, sfx_prompt, music_prompt.

केवल JSON लौटाएँ। JSON के बाहर कोई टिप्पणी न दें।
"""


def validate_text_fields(record, label):
    if not isinstance(record, dict):
        raise ValueError(f"{label} must be an object.")

    result = {}

    for field in REQUIRED_FIELDS:
        value = record.get(field)

        if not isinstance(value, str):
            raise ValueError(
                f"{label}.{field} must be text."
            )

        value = value.strip()

        if field in REQUIRED_NONEMPTY and not value:
            raise ValueError(
                f"{label}.{field} cannot be empty."
            )

        result[field] = value

    return result


def validate_extra_record(record, source_record, role):
    if source_record is None:
        if record is not None:
            raise ValueError(
                f"Unexpected {role} record was generated."
            )
        return None

    if not isinstance(record, dict):
        raise ValueError(
            f"Required separate {role} record is missing."
        )

    result = validate_text_fields(record, role)
    result.update({
        "scene_id": clean(source_record.get("scene_id")),
        "role": role,
        "status": "completed",
        "purpose": clean(source_record.get("purpose")),
        "story_anchor": clean(source_record.get("story_anchor")),
        "scene_duration": clean(
            source_record.get(
                "scene_duration",
                source_record.get("duration", ""),
            )
        ),
    })

    return result


def validate_generated_scenes(
    generated,
    source_part,
    pipeline_config,
):
    if not isinstance(generated, list):
        raise ValueError("Generated main scenes must be a list.")

    expected = source_part.get("scenes", [])

    if len(generated) != len(expected):
        raise ValueError(
            f"Expected {len(expected)} scenes, "
            f"received {len(generated)}."
        )

    actual_numbers = [
        item.get("scene") if isinstance(item, dict) else None
        for item in generated
    ]
    expected_numbers = [item["scene"] for item in expected]

    if actual_numbers != expected_numbers:
        raise ValueError("Main scene numbering or order mismatch.")

    clean_scenes = []

    for source, item in zip(expected, generated):
        fields = validate_text_fields(
            item, f"Scene {source['scene']}"
        )

        row = {
            "scene": source["scene"],
            "role": clean(source.get("role")) or "main",
            "status": "completed",
            "scene_id": clean(source.get("scene_id")),
            "story_anchor": clean(source.get("story_anchor")),
            "purpose": clean(source.get("purpose")),
            "beat": clean(source.get("beat")),
            "scene_duration": clean(
                source.get(
                    "scene_duration",
                    source.get("duration", ""),
                )
            ),
        }
        row.update(fields)
        clean_scenes.append(row)

    count = word_count(
        " ".join(item["narration"] for item in clean_scenes)
    )

    if pipeline_config["format"] == "short":
        if count < SHORT_MIN_WORDS or count > SHORT_HARD_MAX:
            raise ValueError(
                f"SHORT main narration has {count} words; "
                "allowed hard range is 120–180."
            )

        if count > SHORT_TARGET_MAX:
            raise ValueError(
                f"SHORT main narration has {count} words; "
                "revision required to reach 120–150."
            )

    return clean_scenes


def validate_generated_part(
    result,
    source_part,
    pipeline_config,
):
    if not isinstance(result, dict):
        raise ValueError("Model response must be a JSON object.")

    main_scenes = validate_generated_scenes(
        result.get("scenes"),
        source_part,
        pipeline_config,
    )

    main_text = " ".join(
        item["narration"] for item in main_scenes
    )

    completed = {
        "part": source_part["part"],
        "status": "completed",
        "title": clean(source_part.get("title")),
        "narration": main_text,
        "word_count": word_count(main_text),
        "scenes": main_scenes,
        "story_title": clean(result.get("story_title")),
    }

    for key, role in (
        ("hook_scene", "hook"),
        ("suspense_scene", "suspense"),
        ("resolution_scene", "resolution"),
    ):
        source_record = source_part.get(key)
        generated_record = result.get(key)

        completed[key] = validate_extra_record(
            generated_record,
            source_record,
            role,
        )

    return completed


def build_revision_prompt(
    original_prompt,
    result,
    source_part,
    pipeline_config,
):
    current_text = " ".join(
        clean(item.get("narration"))
        for item in result.get("scenes", [])
        if isinstance(item, dict)
    )

    current_count = word_count(current_text)

    return (
        original_prompt
        + "\n\nपिछला उत्तर शब्द-सीमा या गुणवत्ता जाँच में असफल हुआ।"
        + f"\nवर्तमान मुख्य narration शब्द: {current_count}"
        + "\nइसी भाग को सुधारकर पूरा JSON दोबारा लौटाएँ।"
        + "\nकहानी का अर्थ, घटनाएँ, scene IDs और scene क्रम न बदलें।"
        + "\nSHORT में मुख्य narration 120–150 शब्दों में रखें।"
        + "\n151–180 होने पर उसे घटाकर 120–150 करें।"
        + "\nHook/Suspense/Resolution अलग records में ही रखें।"
        + "\nअधूरे वाक्य या मनमाना शब्द-कटाव न करें।"
        + "\nकेवल JSON लौटाएँ।"
    )


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
                f"Part {source_part['part']}/{total_parts}; "
                f"attempt {attempt}/{retries}; model={model}"
            )

            result = request_gemini(
                api_key, model, prompt, schema
            )

            try:
                return validate_generated_part(
                    result,
                    source_part,
                    pipeline_config,
                )
            except ValueError as exc:
                last_error = str(exc)

                if pipeline_config["format"] == "short":
                    prompt = build_revision_prompt(
                        prompt,
                        result,
                        source_part,
                        pipeline_config,
                    )

                raise ValueError(last_error)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8", errors="replace"
            )
            last_error = f"HTTP {exc.code}: {body[:1200]}"
            log(last_error)

            if exc.code not in {
                408, 425, 429, 500, 502, 503, 504
            }:
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
            log(last_error)

        if attempt < retries:
            delay = min(30, 3 * (2 ** (attempt - 1)))
            log(f"Retrying after {delay} seconds.")
            time.sleep(delay)

    raise RuntimeError(
        f"Part {source_part['part']} failed after "
        f"{retries} attempts: {last_error}"
    )


def source_record_signature(part):
    return stable_hash({
        "part": part.get("part"),
        "scenes": part.get("scenes"),
        "hook_scene": part.get("hook_scene"),
        "suspense_scene": part.get("suspense_scene"),
        "resolution_scene": part.get("resolution_scene"),
    })


def build_generation_signature(
    story,
    pipeline_config,
    topic,
    story_text,
    model,
):
    return stable_hash({
        "version": VERSION,
        "source_plan": story,
        "pipeline_config": pipeline_config,
        "topic": topic,
        "story_text": story_text,
        "model": model,
    })


def is_completed_part_valid(
    existing,
    source_part,
    pipeline_config,
):
    if not isinstance(existing, dict):
        return False

    if existing.get("status") != "completed":
        return False

    if existing.get("part") != source_part.get("part"):
        return False

    if existing.get("source_part_signature") != (
        source_record_signature(source_part)
    ):
        return False

    scenes = existing.get("scenes")
    expected = source_part.get("scenes", [])

    if not isinstance(scenes, list) or len(scenes) != len(expected):
        return False

    try:
        for source, scene in zip(expected, scenes):
            if scene.get("scene") != source.get("scene"):
                return False

            if scene.get("scene_id") != source.get("scene_id"):
                return False

            if scene.get("status") != "completed":
                return False

            for field in REQUIRED_NONEMPTY:
                if not clean(scene.get(field)):
                    return False

        main_words = word_count(
            " ".join(
                clean(scene.get("narration"))
                for scene in scenes
            )
        )

        if pipeline_config["format"] == "short":
            if not SHORT_MIN_WORDS <= main_words <= SHORT_TARGET_MAX:
                return False

        for key in (
            "hook_scene",
            "suspense_scene",
            "resolution_scene",
        ):
            source_record = source_part.get(key)
            cached_record = existing.get(key)

            if source_record is None:
                if cached_record is not None:
                    return False
                continue

            if not isinstance(cached_record, dict):
                return False

            if cached_record.get("scene_id") != (
                source_record.get("scene_id")
            ):
                return False

            if cached_record.get("status") != "completed":
                return False

            for field in REQUIRED_NONEMPTY:
                if not clean(cached_record.get(field)):
                    return False

        return True

    except (TypeError, ValueError, KeyError):
        return False


def find_existing_part(parts, part_number):
    for item in parts:
        if (
            isinstance(item, dict)
            and item.get("part") == part_number
        ):
            return item
    return None


def validate_final_output(
    ai_story,
    source_story,
    pipeline_config,
):
    source_parts = source_story.get("parts", [])
    output_parts = ai_story.get("parts", [])

    if len(output_parts) != len(source_parts):
        raise RuntimeError(
            f"Final part count mismatch: "
            f"{len(output_parts)}/{len(source_parts)}"
        )

    total_main_scenes = 0
    all_main_narration = []

    for index, (source, output) in enumerate(
        zip(source_parts, output_parts), 1
    ):
        if source.get("part") != index:
            raise RuntimeError(
                f"Source part order mismatch at part {index}."
            )

        if output.get("part") != index:
            raise RuntimeError(
                f"Output part order mismatch at part {index}."
            )

        if output.get("status") != "completed":
            raise RuntimeError(f"Part {index} is incomplete.")

        expected_scenes = source.get("scenes", [])
        actual_scenes = output.get("scenes", [])

        if len(actual_scenes) != len(expected_scenes):
            raise RuntimeError(
                f"Scene count mismatch in part {index}."
            )

        for expected, actual in zip(
            expected_scenes, actual_scenes
        ):
            if actual.get("scene") != expected.get("scene"):
                raise RuntimeError(
                    f"Scene order mismatch in part {index}."
                )

            if actual.get("scene_id") != expected.get("scene_id"):
                raise RuntimeError(
                    f"Scene ID mismatch in part {index}."
                )

            if actual.get("status") != "completed":
                raise RuntimeError(
                    f"Incomplete scene in part {index}."
                )

            for field in REQUIRED_NONEMPTY:
                if not clean(actual.get(field)):
                    raise RuntimeError(
                        f"Missing {field} in part {index}."
                    )

        main_text = " ".join(
            item["narration"] for item in actual_scenes
        )
        main_words = word_count(main_text)

        if output.get("word_count") != main_words:
            raise RuntimeError(
                f"Incorrect word count metadata in part {index}."
            )

        if pipeline_config["format"] == "short":
            if not SHORT_MIN_WORDS <= main_words <= SHORT_TARGET_MAX:
                raise RuntimeError(
                    f"SHORT part {index} has {main_words} words; "
                    "required range is 120–150."
                )

        final_part = index == len(source_parts)

        expected_roles = {
            "hook_scene": source.get("hook_scene"),
            "suspense_scene": (
                None if final_part
                else source.get("suspense_scene")
            ),
            "resolution_scene": (
                source.get("resolution_scene")
                if final_part else None
            ),
        }

        for key, expected in expected_roles.items():
            actual = output.get(key)

            if expected is None:
                if actual is not None:
                    raise RuntimeError(
                        f"Unexpected {key} in part {index}."
                    )
                continue

            if not isinstance(actual, dict):
                raise RuntimeError(
                    f"Missing {key} in part {index}."
                )

            if actual.get("scene_id") != expected.get("scene_id"):
                raise RuntimeError(
                    f"{key} scene_id mismatch in part {index}."
                )

            if actual.get("role") != key.removesuffix("_scene"):
                raise RuntimeError(
                    f"{key} role mismatch in part {index}."
                )

            if actual.get("status") != "completed":
                raise RuntimeError(
                    f"{key} incomplete in part {index}."
                )

            for field in REQUIRED_NONEMPTY:
                if not clean(actual.get(field)):
                    raise RuntimeError(
                        f"{key} has empty {field} in part {index}."
                    )

        total_main_scenes += len(actual_scenes)
        all_main_narration.append(main_text)

    return (
        len(output_parts),
        total_main_scenes,
        all_main_narration,
    )


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    input_config = load_input_config()
    pipeline_config = build_config()
    model = load_selected_model()
    story = read_json(INPUT)

    total_parts, total_scenes = validate_source_plan(
        story,
        pipeline_config["format"],
    )

    topic = clean(get_topic(input_config)) or clean(
        story.get("topic")
    )

    story_text = clean(get_story_text(input_config)) or clean(
        story.get("story_text")
    )

    if not topic:
        topic = clean(story.get("title")) or "कहानी"

    if not story_text:
        story_text = clean(story.get("story_text"))

    signature = build_generation_signature(
        story,
        pipeline_config,
        topic,
        story_text,
        model,
    )

    existing = read_json(OUTPUT, required=False)

    resumable = (
        isinstance(existing, dict)
        and existing.get("generation_signature") == signature
        and existing.get("status") in {
            "in_progress", "completed"
        }
        and pipeline_config["resume_enabled"]
    )

    if resumable:
        log("Matching checkpoint found; valid parts will be reused.")
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

    ai_story.update({
        "version": VERSION,
        "status": "in_progress",
        "topic": topic,
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

        cached = find_existing_part(
            ai_story.get("parts", []),
            part_number,
        )

        if (
            pipeline_config["skip_completed"]
            and is_completed_part_valid(
                cached,
                source_part,
                pipeline_config,
            )
        ):
            log(
                f"Part {part_number}: "
                "valid checkpoint reused."
            )
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

        completed_part["source_part_signature"] = (
            source_record_signature(source_part)
        )

        current_parts = [
            item
            for item in ai_story.get("parts", [])
            if (
                isinstance(item, dict)
                and item.get("part") != part_number
            )
        ]

        current_parts.append(completed_part)
        current_parts.sort(key=lambda item: item["part"])

        ai_story["parts"] = current_parts
        ai_story["status"] = "in_progress"

        # Save after every successful part.
        write_json(OUTPUT, ai_story)

        log(
            f"Part {part_number}/{total_parts}: "
            "checkpoint saved."
        )

    completed_parts, completed_scenes, part_narrations = (
        validate_final_output(
            ai_story,
            story,
            pipeline_config,
        )
    )

    if completed_parts != total_parts:
        raise RuntimeError("Final part total does not match source plan.")

    if completed_scenes != total_scenes:
        raise RuntimeError("Final scene total does not match source plan.")

    titles = [
        clean(item.get("story_title"))
        for item in ai_story.get("parts", [])
        if clean(item.get("story_title"))
    ]

    if titles:
        normalized_titles = {
            re.sub(r"\s+", " ", title).strip().casefold()
            for title in titles
        }

        if len(normalized_titles) > 1:
            raise RuntimeError(
                "Generated story titles differ between parts."
            )

        ai_story["title"] = titles[0]

    ai_story["status"] = "completed"
    ai_story["total_parts"] = completed_parts
    ai_story["parts_count"] = completed_parts
    ai_story["total_scenes"] = completed_scenes
    ai_story["main_story_text"] = "\n\n".join(
        part_narrations
    )
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
        raise SystemExit(
            f"ERROR: {type(exc).__name__}: {exc}"
        )