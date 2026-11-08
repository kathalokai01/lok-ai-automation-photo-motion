#!/usr/bin/env python3
"""Generate story-grounded AI narration with configured LLM provider fallback."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
from llm_provider_client import generate_text as generate_provider_text
from gemini_api_guard import require_gemini_generation

ROOT = Path(__file__).resolve().parent.parent
STORY_FILE = ROOT / "output/story/story.json"
PLAN_FILE = ROOT / "output/story/story_plan.json"
OUTPUT_FILE = ROOT / "output/story/ai_story.json"
MODEL_FILE = ROOT / "output/config/selected_model.json"
LLM_PROVIDER_FILE = ROOT / "output/config/selected_llm_provider.json"

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
REQUEST_TIMEOUT = 180
MAX_RESPONSE_CHARS = 160000
VERSION = "ai-story-v23-canonical-story-title-across-parts"

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
    "narration", "dialogue", "visual_prompt", "negative_prompt",
    "camera_prompt", "lighting_prompt", "sfx_prompt", "music_prompt",
)
REQUIRED_NONEMPTY = (
    "narration", "visual_prompt", "camera_prompt", "lighting_prompt",
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
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
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
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "n", "off", "disabled"}:
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
        model = model[7:]
    if not re.fullmatch(r"gemini-[A-Za-z0-9._-]+", model, re.IGNORECASE):
        return ""
    return model


def validate_provider_model(provider: str, model: str) -> str:
    """Validate a selected provider/model pair before any network request."""
    provider = clean(provider).lower()
    model = clean(model)
    if provider not in {"groq", "openrouter", "cloudflare", "mistral"}:
        raise RuntimeError(f"Unsupported LLM provider: {provider!r}.")
    if not model or len(model) > 200 or any(ch.isspace() for ch in model):
        raise RuntimeError(f"Invalid model identifier for {provider}.")
    if provider == "cloudflare" and not model.startswith("@cf/"):
        raise RuntimeError("Cloudflare model must use the @cf/ namespace.")
    return model


def load_configured_provider_order() -> list[str]:
    data = read_json(LLM_PROVIDER_FILE, required=False)
    if not isinstance(data, dict):
        log("No configured LLM provider selection file was found.")
        return []

    if (
        data.get("enabled") is not True
        or data.get("status") != "ready"
    ):
        log("LLM provider selector reports no ready fallback.")
        return []

    order = data.get("provider_order")
    records = data.get("providers")

    if not isinstance(order, list) or not isinstance(records, list):
        raise RuntimeError("Invalid LLM provider selection schema.")

    model_by_provider = {}
    for item in records:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid provider record in LLM selector.")
        name = clean(item.get("provider")).lower()
        if name not in {"groq", "openrouter", "cloudflare", "mistral"}:
            raise RuntimeError(f"Unsupported provider record: {name!r}.")
        if item.get("ready") is True:
            if name in model_by_provider:
                raise RuntimeError(f"Duplicate ready provider record: {name}.")
            model_by_provider[name] = validate_provider_model(
                name, item.get("model")
            )

    result = []
    seen = set()
    for provider in order:
        name = clean(provider).lower()
        if name not in {"groq", "openrouter", "cloudflare", "mistral"}:
            raise RuntimeError(f"Invalid provider order entry: {name!r}.")
        if name in seen:
            continue
        seen.add(name)
        model = model_by_provider.get(name)
        if model:
            result.append(f"{name}|{model}")

    if data.get("status") == "ready" and not result:
        log("LLM provider selector has no ready providers.")
    return result


def load_model_order() -> list[str]:
    """Keep configured Gemini order first, then configured external providers."""
    order = []
    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    gemini_allowed = os.environ.get("ALLOW_GEMINI_GENERATION", "").strip() == "I_ACCEPT_POTENTIAL_GEMINI_CHARGES"

    if api_key and gemini_allowed:
        data = read_json(MODEL_FILE, required=False)
        if isinstance(data, dict) and data.get("status") == "selected":
            primary = normalize_model(data.get("model"))
            saved_order = data.get("fallback_order")
            if not primary:
                raise RuntimeError("Selected Gemini primary model is invalid.")
            if (
                not isinstance(saved_order, list)
                or not saved_order
                or normalize_model(saved_order[0]) != primary
            ):
                raise RuntimeError(
                    "Selected Gemini fallback_order is invalid or reordered."
                )

            configured = configured_fallbacks(data)
            if configured[0] != primary:
                raise RuntimeError(
                    "Configured Gemini priority differs from selector output."
                )

            for value in configured:
                model = normalize_model(value)
                if not model:
                    raise RuntimeError("Invalid model in configured Gemini order.")
                item = f"gemini|{model}"
                if item not in order:
                    order.append(item)
        else:
            log("Gemini model selection file unavailable; skipping Gemini.")
    elif api_key:
        log("Gemini skipped: explicit billable-generation opt-in is absent.")
    else:
        log("Gemini API key is absent; skipping Gemini.")

    provider_order = load_configured_provider_order()
    order.extend(provider_order)

    if not order:
        raise RuntimeError(
            "No configured LLM provider is ready."
        )

    log("Configured LLM model order:")
    for index, item in enumerate(order, 1):
        provider, model = item.split("|", 1)
        log(f"  {index}. {provider}: {model}")

    return order

def validate_source_extra(record: Any, role: str, part_number: int) -> None:
    if not isinstance(record, dict):
        raise RuntimeError(f"Part {part_number}: invalid {role} source record.")
    if clean(record.get("role")).lower() != role:
        raise RuntimeError(f"Part {part_number}: {role} source role mismatch.")
    if not clean(record.get("scene_id")):
        raise RuntimeError(f"Part {part_number}: {role} has no scene_id.")
    if not clean(record.get("story_anchor")):
        raise RuntimeError(f"Part {part_number}: {role} has no story_anchor.")


def validate_source_plan(plan: dict, expected_format: str):
    if plan.get("status") not in {"planned", "completed", "in_progress"}:
        raise RuntimeError(f"Invalid story-plan status: {plan.get('status')!r}")
    if clean(plan.get("format")).lower() != expected_format:
        raise RuntimeError("Story-plan format does not match Input/topic.txt.")

    parts = plan.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Story plan has no parts.")
    if plan.get("parts_count") not in (None, len(parts)):
        raise RuntimeError("Story-plan parts_count mismatch.")

    all_ids = set()
    total_main = 0

    for number, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != number:
            raise RuntimeError(f"Invalid source part numbering: {number}")

        scenes = part.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError(f"Part {number} has no main scenes.")

        for scene_number, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict) or scene.get("scene") != scene_number:
                raise RuntimeError(f"Part {number}: main scene order mismatch.")

            scene_id = clean(scene.get("scene_id"))
            if not scene_id or scene_id in all_ids:
                raise RuntimeError(f"Missing or duplicate scene_id: {scene_id!r}")
            if clean(scene.get("role")).lower() not in {"", "main"}:
                raise RuntimeError(f"Main scene has invalid role: {scene_id}")
            if not clean(scene.get("story_anchor")):
                raise RuntimeError(f"Main scene has no story_anchor: {scene_id}")

            all_ids.add(scene_id)

        total_main += len(scenes)
        final = number == len(parts)

        for key, role in ROLE_KEYS.items():
            record = part.get(key)
            required = (
                key == "hook_scene"
                or (key == "suspense_scene" and not final)
                or (key == "resolution_scene" and final)
            )

            if required:
                validate_source_extra(record, role, number)
                scene_id = clean(record.get("scene_id"))
                if scene_id in all_ids:
                    raise RuntimeError(f"Duplicate source scene_id: {scene_id}")
                all_ids.add(scene_id)
            elif record is not None:
                raise RuntimeError(f"Unexpected {key} in part {number}.")

    counts = [len(part["scenes"]) for part in parts]
    if plan.get("actual_scene_counts") != counts:
        raise RuntimeError("Story-plan actual_scene_counts mismatch.")
    if plan.get("total_main_scenes") not in (None, total_main):
        raise RuntimeError("Story-plan total_main_scenes mismatch.")
    if plan.get("total_scenes") not in (None, total_main):
        raise RuntimeError("Story-plan total_scenes mismatch.")

    return len(parts), total_main


def apply_story_part_metadata(source_plan: dict, source_story: dict) -> None:
    story_parts = source_story.get("parts")
    plan_parts = source_plan["parts"]

    if not isinstance(story_parts, list) or len(story_parts) != len(plan_parts):
        raise RuntimeError("Story and Story Plan have different part counts.")

    for number, (story_part, plan_part) in enumerate(
        zip(story_parts, plan_parts), 1
    ):
        if not isinstance(story_part, dict) or story_part.get("part") != number:
            raise RuntimeError(f"Invalid corrected story part {number}.")

        merged = story_part.get("final_part_merged") is True
        if merged and number != len(plan_parts):
            raise RuntimeError("final_part_merged may only be true for final part.")

        plan_part["final_part_merged"] = merged


def scene_context(part: dict) -> list:
    return [{
        "scene": item["scene"],
        "scene_id": clean(item.get("scene_id")),
        "role": "main",
        "beat": clean(item.get("beat")),
        "purpose": clean(item.get("purpose")),
        "story_anchor": clean(item.get("story_anchor")),
        "visual": clean(item.get("visual") or item.get("visual_prompt")),
    } for item in part["scenes"]]


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


def short_word_limits(part: dict, config: dict, total_parts: int):
    if config["format"] != "short":
        return None, None

    if part["part"] == total_parts and part.get("final_part_merged") is True:
        return SHORT_MIN_WORDS, SHORT_FINAL_MERGED_MAX

    return SHORT_MIN_WORDS, SHORT_HARD_MAX


def build_response_schema(part: dict) -> dict:
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
        if part.get(key) is not None:
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
    value = "\n".join(
        item["text"]
        for item in parts
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ).strip()

    if not value:
        raise ValueError("Gemini returned empty content.")
    if len(value) > MAX_RESPONSE_CHARS:
        raise ValueError("Gemini response exceeded the size limit.")

    return value


def parse_json_response(response_text: str) -> dict:
    if not isinstance(response_text, str) or not response_text.strip():
        raise ValueError("Model response is empty or not text.")
    if len(response_text) > MAX_RESPONSE_CHARS:
        raise ValueError("Model response exceeded the configured size limit.")

    value = re.sub(
        r"^\s*```(?:json)?\s*|\s*```\s*$",
        "",
        response_text.strip(),
        flags=re.IGNORECASE,
    ).strip()

    def reject_duplicate_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key!r}.")
            result[key] = item
        return result

    def reject_nonfinite(value):
        raise ValueError(f"Invalid non-finite JSON number: {value}.")

    decoder_options = {
        "object_pairs_hook": reject_duplicate_keys,
        "parse_constant": reject_nonfinite,
    }
    try:
        result = json.loads(value, **decoder_options)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Model response contains no JSON object.")
        result = json.loads(
            value[start:end + 1],
            **decoder_options,
        )

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
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.55,
            "topP": 0.90,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8"),
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
    final = number == total_parts
    merged = final and part.get("final_part_merged") is True
    minimum, maximum = short_word_limits(part, config, total_parts)

    if config["format"] == "short":
        if merged:
            rules = f"मुख्य narration {minimum}–{maximum} शब्दों में रखें।"
        else:
            rules = (
                "मुख्य narration 120–150 शब्द लक्ष्य रखें। "
                "सभी revision प्रयासों के बाद 151–180 शब्द स्वीकार्य हैं। "
                "120 से कम या 180 से अधिक अमान्य है।"
            )
    else:
        rules = (
            "FULL format: सभी महत्वपूर्ण घटनाएँ और मूल ending सुरक्षित रखें।"
        )

    hook = extra_context(part, "hook_scene")
    suspense = None if final else extra_context(part, "suspense_scene")
    resolution = extra_context(part, "resolution_scene") if final else None

    return f"""
आप हिंदी कहानी-संपादन और सिनेमैटिक नैरेशन इंजन हैं।

विषय: {topic}
पूरी मूल और संशोधित कहानी:
{story_text}

भाग: {number}/{total_parts}
Format: {config['format']}
दर्शक: {config['audience']}
कहानी अवधि: {config['story_length']}
दृश्य अवधि: {config['scene_duration']}

इस भाग के निश्चित मुख्य दृश्य और क्रम:
{json.dumps(scene_context(part), ensure_ascii=False, indent=2)}

अलग Hook source:
{json.dumps(hook, ensure_ascii=False, indent=2)}

अलग Suspense source:
{json.dumps(suspense, ensure_ascii=False, indent=2)}

अलग Final Resolution source:
{json.dumps(resolution, ensure_ascii=False, indent=2)}

WORD RULE:
{rules}

कहानी के अनिवार्य नियम:
- Story Plan में तय Part boundaries, scene_id और scene order न बदलें।
- इस Part की घटनाएँ उसी क्रम में रखें जिस क्रम में Plan में हैं।
- पूरी कहानी के संदर्भ से narration लिखें, लेकिन दूसरे Part की घटनाएँ यहाँ न डालें।
- मूल कहानी का अर्थ, घटनाएँ, पात्र, नाम, संबंध, स्थान और परिणाम सुरक्षित रखें।
- कोई नई मुख्य घटना, पात्र या असंबंधित मोड़ न जोड़ें।
- हिंदी मात्रा, वर्तनी, व्याकरण, लिंग, वचन और काल ठीक करें।
- मुख्य narration में Hook, Suspense या Resolution न मिलाएँ।
- हर scene narration उसके story_anchor से मेल खाए।
- वाक्य को बीच में न काटें।
- कहानी-विशिष्ट वास्तविक नाम, स्थान, वस्तुएँ, पशु, वाहन और कपड़े शामिल करें।
- सभी Parts में recurring पात्रों का रूप एक जैसा रखें।

HOOK:
- Source मौजूद हो तो अलग hook_scene record दें।
- कहानी से जुड़ा वास्तविक Hook हो, झूठा clickbait नहीं।

SUSPENSE:
- केवल non-final Part में source मौजूद हो तो अलग suspense_scene दें।
- अंतिम Part में suspense_scene न दें।

RESOLUTION:
- केवल अंतिम Part में source मौजूद हो तो अलग resolution_scene दें।
- मूल कहानी का वास्तविक समाधान दें।

शीर्षक:
- पूरी कहानी के अर्थ और परिणाम पर आधारित एक समान story_title दें।
- सभी Parts में story_title बिल्कुल समान होना चाहिए।

Visual style: {config['visual_style']}
Realism: {config['realism']}
Camera: {config['camera_style']}
Lighting: {config['lighting']}
Mood: {config['mood']}
Quality: {config['quality']}
Avoid cartoon: {config['avoid_cartoon']}
Avoid neon: {config['avoid_neon']}
Avoid glitch: {config['avoid_glitch']}
Natural motion: {config['natural_motion']}
Realistic lighting: {config['realistic_lighting']}
Music: {config['music']}
Music style: {config['music_style']}
SFX: {config['sfx']}
Ambient: {config['ambient_sound']}

हर main scene में अनिवार्य fields:
scene, scene_id, role, narration, dialogue, visual_prompt,
negative_prompt, camera_prompt, lighting_prompt, sfx_prompt, music_prompt.

हर उपलब्ध extra record में अनिवार्य fields:
scene_id, role, narration, dialogue, visual_prompt,
negative_prompt, camera_prompt, lighting_prompt, sfx_prompt, music_prompt.

केवल JSON लौटाएँ। अतिरिक्त explanation न दें।
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

    unexpected = (
        set(record) - set(REQUIRED_FIELDS) - {"scene", "scene_id", "role"}
    )
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
    allow_soft_overage: bool = False,
) -> list:
    if not isinstance(generated, list):
        raise ValueError("Generated main scenes must be a list.")

    expected = source_part["scenes"]
    if len(generated) != len(expected):
        raise ValueError(
            f"Part {source_part['part']}: expected {len(expected)} main scenes, "
            f"received {len(generated)}."
        )

    result = []
    for source, item in zip(expected, generated):
        if not isinstance(item, dict):
            raise ValueError("Generated main scene is not an object.")

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
        and not allow_soft_overage
    ):
        raise ValueError(
            f"SHORT non-final part {source_part['part']} has {count} words; "
            "revise toward 120–150. 151–180 is allowed after retries."
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
            f"{role} scene_id mismatch in part {part_number}."
        )

    if clean(generated.get("role")).lower() != role:
        raise ValueError(f"{role} role mismatch in part {part_number}.")

    fields = validate_text_fields(
        generated, f"Part {part_number} {role}"
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
    allow_soft_overage: bool = False,
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
            "Unexpected generated fields: " + ", ".join(sorted(unexpected))
        )
    if missing:
        raise ValueError(
            "Missing generated fields: " + ", ".join(sorted(missing))
        )

    title = clean(generated.get("story_title"))
    if not title:
        raise ValueError("Generated story_title cannot be empty.")

    scenes = validate_generated_scenes(
        generated.get("scenes"),
        source_part,
        config,
        total_parts,
        allow_soft_overage=allow_soft_overage,
    )

    narration = " ".join(item["narration"] for item in scenes)
    number = source_part["part"]
    final = number == total_parts

    completed = {
        "part": number,
        "status": "completed",
        "title": clean(source_part.get("title")),
        "story_title": title,
        "narration": narration,
        "word_count": word_count(narration),
        "final_part_merged": bool(source_part.get("final_part_merged")),
        "scenes": scenes,
    }

    for key, role in ROLE_KEYS.items():
        source = source_part.get(key)

        if key == "suspense_scene" and final:
            source = None
        if key == "resolution_scene" and not final:
            source = None

        completed[key] = validate_extra_record(
            generated.get(key), source, role, number
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
    current = " ".join(
        clean(item.get("narration"))
        for item in scenes
        if isinstance(item, dict)
    )

    count = word_count(current)
    minimum, maximum = short_word_limits(
        source_part, config, total_parts
    )

    if minimum is None:
        rule = "FULL: preserve complete story content."
        correction = "Remove only repetition and unclear wording."
    else:
        merged = (
            source_part.get("part") == total_parts
            and source_part.get("final_part_merged") is True
        )
        rule = f"Required main narration: {minimum}–{maximum} words."

        if count > maximum:
            correction = (
                f"Current count {count}. Reduce to at most {maximum} words. "
                "Preserve every essential event and the complete ending."
            )
        elif count > SHORT_TARGET_MAX and not merged:
            correction = (
                f"Current count {count}. Reduce toward 135–145 words. "
                "Remove repetition and redundant descriptions only."
            )
        elif count < minimum:
            correction = (
                f"Current count {count}. Expand using relevant details "
                "already present in the source story. Do not invent events."
            )
        else:
            correction = "Keep the narration within the required range."

    return (
        original_prompt
        + "\n\nपिछला उत्तर validation में असफल हुआ।"
        + f"\nवर्तमान main narration शब्द: {count}"
        + f"\nनियम: {rule}"
        + f"\nसुधार: {correction}"
        + "\nयह नया कहानी-लेखन नहीं, मौजूदा उत्तर का सुधार है।"
        + "\nHook/Suspense/Resolution की शब्द संख्या main narration में न गिनें।"
        + "\nसभी scene_id, role और scene क्रम source plan के समान रखें।"
        + "\nकहानी का अर्थ, घटनाएँ, पात्र और मूल अंत न बदलें।"
        + "\nवाक्य को बीच में न काटें।"
        + "\nकेवल JSON लौटाएँ।"
    )


def retryable_http_error(code: int) -> bool:
    return code in {408, 425, 429, 500, 502, 503, 504}


def retry_delay(code: int, attempt: int) -> int:
    base = 12 if code == 429 else 8
    return min(60, base * (2 ** (attempt - 1)))


def request_selected_model(
    api_key: str,
    selected_model: str,
    prompt: str,
    schema: dict,
) -> dict:
    if "|" not in selected_model:
        raise ValueError(
            f"Invalid selected model identifier: {selected_model!r}"
        )

    provider, model = selected_model.split("|", 1)
    provider = provider.strip().lower()
    model = model.strip()

    if not provider or not model:
        raise ValueError(
            f"Invalid selected model identifier: {selected_model!r}"
        )

    if provider == "gemini":
        if not api_key:
            raise RuntimeError(
                "Gemini was selected but no Gemini API key is configured."
            )
        authorized_key = require_gemini_generation("AI story narration", api_key=api_key)
        parsed = request_gemini(authorized_key, model, prompt, schema)
        return parsed

    validate_provider_model(provider, model)

    result = generate_provider_text(
        provider=provider,
        model=model,
        prompt=prompt,
        max_tokens=12000,
        temperature=0.55,
        timeout=REQUEST_TIMEOUT,
    )

    if not isinstance(result, dict):
        raise ValueError(
            f"{provider} returned an invalid client response."
        )

    response_text = result.get("text")
    if not isinstance(response_text, str) or not response_text.strip():
        raise ValueError(
            f"{provider} returned an empty model response."
        )

    parsed = parse_json_response(response_text)
    parsed["_katha_provider_used"] = provider
    parsed["_katha_model_used"] = model
    return parsed


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
        topic, story_text, source_part, config, total_parts
    )
    schema = build_response_schema(source_part)
    errors = []

    for model_index, selected_model in enumerate(models, 1):
        prompt = original_prompt
        model_errors = []
        last_generated = None
        max_attempts = max(1, int(config["max_retries"]))

        log(
            f"Part {source_part['part']}/{total_parts}: "
            f"provider/model {model_index}/{len(models)}: {selected_model}"
        )

        for attempt in range(1, max_attempts + 1):
            try:
                log(
                    f"Part {source_part['part']}; "
                    f"model={selected_model}; "
                    f"attempt {attempt}/{max_attempts}"
                )

                generated = request_selected_model(
                    api_key, selected_model, prompt, schema
                )
                last_generated = generated

                try:
                    completed = validate_generated_part(
                        generated,
                        source_part,
                        config,
                        total_parts,
                        allow_soft_overage=False,
                    )

                    provider, actual_model = selected_model.split("|", 1)
                    completed["_katha_provider_used"] = provider
                    completed["_katha_model_used"] = actual_model

                    log(
                        f"Part {source_part['part']}: "
                        f"{completed['word_count']} words accepted "
                        f"on {selected_model}."
                    )
                    return completed

                except (ValueError, TypeError, KeyError) as exc:
                    message = f"Validation attempt {attempt}: {exc}"
                    model_errors.append(message)
                    log(f"Part {source_part['part']} {selected_model}: {message}")

                    prompt = build_revision_prompt(
                        original_prompt,
                        generated,
                        source_part,
                        config,
                        total_parts,
                    )

            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                model_errors.append(message)
                log(f"Model {selected_model}: {message}")

                retryable = (
                    isinstance(exc, (TimeoutError, ConnectionError))
                    or (
                        isinstance(exc, urllib.error.URLError)
                    )
                    or (
                        getattr(exc, "retryable", False) is True
                    )
                )

                if retryable and attempt < max_attempts:
                    status = getattr(exc, "status_code", None)
                    delay = (
                        retry_delay(status, attempt)
                        if status is not None
                        else min(30, 5 * (2 ** (attempt - 1)))
                    )
                    log(
                        f"Temporary failure; retrying same model "
                        f"in {delay}s."
                    )
                    time.sleep(delay)
                    continue

                if isinstance(exc, (ValueError, TypeError, KeyError)):
                    if attempt < max_attempts:
                        delay = min(30, 3 * (2 ** (attempt - 1)))
                        log(f"Retrying {selected_model} in {delay}s.")
                        time.sleep(delay)
                        continue

                break

        # A non-final SHORT part may use 151–180 words only after
        # all normal validation/revision attempts for this model.
        if last_generated is not None:
            try:
                completed = validate_generated_part(
                    last_generated,
                    source_part,
                    config,
                    total_parts,
                    allow_soft_overage=True,
                )

                if (
                    config["format"] == "short"
                    and source_part["part"] < total_parts
                    and SHORT_TARGET_MAX < completed["word_count"] <= SHORT_HARD_MAX
                ):
                    provider, actual_model = selected_model.split("|", 1)
                    completed["_katha_provider_used"] = provider
                    completed["_katha_model_used"] = actual_model

                    log(
                        f"Part {source_part['part']}: accepting "
                        f"{completed['word_count']} words after retries."
                    )
                    return completed

            except (ValueError, TypeError, KeyError) as exc:
                model_errors.append(f"Final validation: {exc}")

        errors.append({
            "model": selected_model,
            "errors": model_errors[-max_attempts:],
        })

        log(
            f"Provider/model exhausted: {selected_model}. "
            "Moving to the next configured LLM option."
        )

    summary = json.dumps(errors, ensure_ascii=False)
    raise RuntimeError(
        f"Part {source_part['part']} failed: all configured LLM "
        f"models were exhausted. "
        f"Details: {summary[:5000]}"
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
    if existing.get("source_part_signature") != source_part_signature(source_part):
        return False
    if bool(existing.get("final_part_merged")) != bool(source_part.get("final_part_merged")):
        return False

    try:
        expected = source_part["scenes"]
        actual = existing.get("scenes")

        if not isinstance(actual, list) or len(actual) != len(expected):
            return False

        for source, row in zip(expected, actual):
            if not isinstance(row, dict):
                return False
            if row.get("scene") != source.get("scene"):
                return False
            if row.get("scene_id") != source.get("scene_id"):
                return False
            if clean(row.get("story_anchor")) != clean(source.get("story_anchor")):
                return False
            if row.get("role") != "main" or row.get("status") != "completed":
                return False

            for field in REQUIRED_NONEMPTY:
                if not clean(row.get(field)):
                    return False

        count = word_count(
            " ".join(clean(row.get("narration")) for row in actual)
        )
        minimum, maximum = short_word_limits(
            source_part, config, total_parts
        )

        if minimum is not None and not minimum <= count <= maximum:
            return False

        final = source_part["part"] == total_parts

        for key, role in ROLE_KEYS.items():
            source = source_part.get(key)

            if key == "suspense_scene" and final:
                source = None
            if key == "resolution_scene" and not final:
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
            if clean(cached.get("story_anchor")) != clean(source.get("story_anchor")):
                return False
            if cached.get("role") != role or cached.get("status") != "completed":
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
        if isinstance(item, dict) and item.get("part") == part_number
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

    counts = [len(part.get("scenes", [])) for part in source_parts]

    if ai_story.get("actual_scene_counts") != counts:
        raise RuntimeError("AI story actual_scene_counts mismatch.")

    total_main = 0
    narrations = []
    titles = set()

    for index, (source, output) in enumerate(
        zip(source_parts, output_parts), 1
    ):
        if source.get("part") != index or output.get("part") != index:
            raise RuntimeError(f"Part order mismatch at part {index}.")

        if output.get("status") != "completed":
            raise RuntimeError(f"Part {index} is incomplete.")

        if output.get("source_part_signature") != source_part_signature(source):
            raise RuntimeError(
                f"Part {index} does not match final Story Plan."
            )

        if bool(output.get("final_part_merged")) != bool(source.get("final_part_merged")):
            raise RuntimeError(
                f"Part {index} final_part_merged mismatch."
            )

        expected = source["scenes"]
        actual = output.get("scenes")

        if not isinstance(actual, list) or len(actual) != len(expected):
            raise RuntimeError(
                f"Part {index} main scene count mismatch."
            )

        for source_scene, row in zip(expected, actual):
            if row.get("scene") != source_scene.get("scene"):
                raise RuntimeError(
                    f"Part {index} scene order mismatch."
                )
            if row.get("scene_id") != source_scene.get("scene_id"):
                raise RuntimeError(
                    f"Part {index} scene_id mismatch."
                )
            if clean(row.get("story_anchor")) != clean(source_scene.get("story_anchor")):
                raise RuntimeError(
                    f"Part {index} scene story_anchor mismatch."
                )
            if row.get("role") != "main" or row.get("status") != "completed":
                raise RuntimeError(
                    f"Part {index} main scene status/role mismatch."
                )

            for field in REQUIRED_NONEMPTY:
                if not clean(row.get(field)):
                    raise RuntimeError(
                        f"Part {index} scene missing {field}."
                    )

        main_text = " ".join(row["narration"] for row in actual)
        count = word_count(main_text)

        if output.get("word_count") != count:
            raise RuntimeError(
                f"Part {index} word_count metadata mismatch."
            )

        minimum, maximum = short_word_limits(
            source, config, len(source_parts)
        )

        if minimum is not None and not minimum <= count <= maximum:
            raise RuntimeError(
                f"SHORT part {index} has {count} words; "
                f"required {minimum}–{maximum}."
            )

        final = index == len(source_parts)

        for key, role in ROLE_KEYS.items():
            expected_extra = source.get(key)

            if key == "suspense_scene" and final:
                expected_extra = None
            if key == "resolution_scene" and not final:
                expected_extra = None

            actual_extra = output.get(key)

            if expected_extra is None:
                if actual_extra is not None:
                    raise RuntimeError(
                        f"Unexpected {role} record in part {index}."
                    )
                continue

            if not isinstance(actual_extra, dict):
                raise RuntimeError(
                    f"Missing separate {role} record in part {index}."
                )

            if actual_extra.get("scene_id") != expected_extra.get("scene_id"):
                raise RuntimeError(
                    f"{role} scene_id mismatch in part {index}."
                )

            if (
                actual_extra.get("role") != role
                or actual_extra.get("status") != "completed"
            ):
                raise RuntimeError(
                    f"{role} role/status mismatch in part {index}."
                )

            if clean(actual_extra.get("story_anchor")) != clean(expected_extra.get("story_anchor")):
                raise RuntimeError(
                    f"{role} story_anchor mismatch in part {index}."
                )

            for field in REQUIRED_NONEMPTY:
                if not clean(actual_extra.get(field)):
                    raise RuntimeError(
                        f"{role} record missing {field} in part {index}."
                    )

        title = clean(output.get("story_title"))

        if not title:
            raise RuntimeError(f"Part {index} has no story_title.")

        titles.add(re.sub(r"\s+", " ", title).casefold())
        total_main += len(actual)
        narrations.append(main_text)

    if len(titles) != 1:
        raise RuntimeError(
            "Generated story_title differs between parts."
        )

    return len(output_parts), total_main, narrations


def main() -> None:
    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )

    input_config = load_input_config()
    config = build_config()
    models = load_model_order()

    if not models:
        raise RuntimeError(
            "No configured LLM model is available. Check provider credentials, model settings, provider order, and Gemini billing opt-in."
        )

    primary_model = models[0]

    source_story = read_json(STORY_FILE)
    source_plan = read_json(PLAN_FILE)

    total_parts, total_scenes = validate_source_plan(
        source_plan, config["format"]
    )

    # Keep the signature tied to the exact on-disk Story Plan. The
    # metadata enrichment below mutates the in-memory plan for per-Part
    # validation, so hashing after enrichment makes downstream consumers
    # reject an otherwise matching story_plan.json file.
    source_plan_file_signature = stable_hash(source_plan)
    apply_story_part_metadata(source_plan, source_story)
    legacy_enriched_plan_signature = stable_hash(source_plan)

    canonical_story_title = clean(
        source_story.get("final_title")
        or source_story.get("generated_title")
        or source_story.get("story_title")
        or source_story.get("title")
        or source_plan.get("final_title")
        or source_plan.get("title")
    )
    if not canonical_story_title:
        raise RuntimeError(
            "Canonical story title is missing from the corrected story. "
            "Refusing to generate different titles for individual Parts."
        )

    topic = (
        clean(get_topic(input_config))
        or clean(source_story.get("topic"))
        or clean(source_story.get("final_title"))
        or clean(source_story.get("title"))
        or clean(source_plan.get("final_title"))
        or "कहानी"
    )

    story_text = (
        clean(source_story.get("corrected_complete_story"))
        or clean(source_story.get("story_text"))
        or clean(get_story_text(input_config))
    )

    if not story_text:
        raise RuntimeError(
            "Final corrected story text is missing from story.json/Input."
        )

    story_signature = stable_hash(source_story)
    plan_signature = source_plan_file_signature

    signature = build_generation_signature(
        {
            "story_signature": story_signature,
            "plan_signature": plan_signature,
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
        and existing.get("source_story_signature") == story_signature
        and existing.get("source_plan_signature") == plan_signature
        and existing.get("status") in {"in_progress", "completed"}
        and config["resume_enabled"]
    )

    if resumable:
        ai_story = existing
        log("Matching story and plan checkpoint found.")
    else:
        reusable = []

        # Older runs hashed the in-memory plan after adding
        # final_part_merged metadata. Accept that legacy fingerprint only
        # when the story, generation settings, and every reused Part are
        # independently verified against the current source files.
        legacy_artifact_compatible = (
            isinstance(existing, dict)
            and existing.get("version") == VERSION
            and existing.get("source_story_signature") == story_signature
            and existing.get("source_plan_signature")
            == legacy_enriched_plan_signature
            and existing.get("input_config") == config
            and clean(existing.get("topic")) == topic
            and clean(existing.get("story_text")) == story_text
            and clean(existing.get("format")).lower() == config["format"]
            and clean(existing.get("audience")) == config["audience"]
            and clean(existing.get("story_length")) == config["story_length"]
            and clean(existing.get("scene_duration")) == config["scene_duration"]
            and isinstance(existing.get("parts"), list)
        )

        if legacy_artifact_compatible:
            for source_part in source_plan["parts"]:
                cached_part = find_existing_part(
                    existing["parts"], source_part["part"]
                )
                if is_completed_part_valid(
                    cached_part, source_part, config, total_parts
                ):
                    reusable.append(cached_part)

            log(
                "Legacy AI Story checkpoint verified: "
                f"reusing {len(reusable)}/{total_parts} completed Parts."
            )

        ai_story = {
            "version": VERSION,
            "status": "in_progress",
            "topic": topic,
            "title": canonical_story_title,
            "story_title": canonical_story_title,
            "story_text": story_text,
            "format": config["format"],
            "audience": config["audience"],
            "story_length": config["story_length"],
            "scene_duration": config["scene_duration"],
            "model": primary_model,
            "generation_signature": signature,
            "source_story_signature": story_signature,
            "source_plan_signature": plan_signature,
            "input_config": config,
            "actual_scene_counts": [
                len(part["scenes"]) for part in source_plan["parts"]
            ],
            "parts": reusable,
        }

        write_json(OUTPUT_FILE, ai_story)

    ai_story.update({
        "version": VERSION,
        "status": "in_progress",
        "topic": topic,
        "title": canonical_story_title,
        "story_title": canonical_story_title,
        "story_text": story_text,
        "format": config["format"],
        "audience": config["audience"],
        "story_length": config["story_length"],
        "scene_duration": config["scene_duration"],
        "model": primary_model,
        "model_order": models,
        "generation_signature": signature,
        "source_story_signature": story_signature,
        "source_plan_signature": plan_signature,
        "input_config": config,
        "actual_scene_counts": [
            len(part["scenes"]) for part in source_plan["parts"]
        ],
    })

    log("=" * 58)
    log("AI STORY GENERATION")
    log(f"Primary provider/model: {primary_model}")
    log(f"Configured fallback count: {len(models) - 1}")
    log(f"Format: {config['format']}")
    log(f"Parts: {total_parts}")
    log(f"Main scenes: {total_scenes}")
    log("=" * 58)

    cached_parts = ai_story.get("parts", [])

    if not isinstance(cached_parts, list):
        cached_parts = []

    # Normalize older reusable Part checkpoints to the corrected story title.
    for cached_part in cached_parts:
        if isinstance(cached_part, dict):
            cached_part["story_title"] = canonical_story_title

    ai_story["parts"] = cached_parts
    ai_story["title"] = canonical_story_title
    ai_story["story_title"] = canonical_story_title
    write_json(OUTPUT_FILE, ai_story)

    for source_part in source_plan["parts"]:
        number = source_part["part"]
        cached = find_existing_part(cached_parts, number)

        if config["skip_completed"] and is_completed_part_valid(
            cached, source_part, config, total_parts
        ):
            log(f"Part {number}: valid checkpoint reused.")
            continue

        completed = generate_part(
            api_key,
            models,
            topic,
            story_text,
            source_part,
            config,
            total_parts,
        )

        if not isinstance(completed, dict):
            raise RuntimeError(
                f"Part {number}: generator returned invalid result; "
                "refusing to continue."
            )

        # Model-generated titles are not authoritative at Part level.
        completed["story_title"] = canonical_story_title

        used_provider = completed.pop("_katha_provider_used", None)
        used_model = completed.pop("_katha_model_used", None)

        if not used_provider or not used_model:
            raise RuntimeError(
                f"Part {number}: generated provider/model identity is missing."
            )

        completed["generated_by_provider"] = used_provider
        completed["generated_by_model"] = used_model
        completed["source_part_signature"] = source_part_signature(
            source_part
        )

        cached_parts = [
            item for item in cached_parts
            if not (
                isinstance(item, dict)
                and item.get("part") == number
            )
        ]

        cached_parts.append(completed)
        cached_parts.sort(key=lambda item: item["part"])

        ai_story["parts"] = cached_parts
        ai_story["status"] = "in_progress"
        ai_story["last_successful_provider"] = used_provider
        ai_story["last_successful_model"] = used_model

        write_json(OUTPUT_FILE, ai_story)

        log(
            f"Part {number}/{total_parts}: checkpoint saved "
            f"(provider={used_provider}, model={used_model})."
        )

    ai_story["parts"] = cached_parts

    completed_parts, completed_scenes, narrations = validate_final_output(
        ai_story, source_plan, config
    )

    if completed_parts != total_parts:
        raise RuntimeError(
            "Final part count does not match Story Plan."
        )

    if completed_scenes != total_scenes:
        raise RuntimeError(
            "Final main scene count does not match Story Plan."
        )

    for item in ai_story["parts"]:
        if not isinstance(item, dict):
            raise RuntimeError("AI story contains an invalid Part record.")
        item["story_title"] = canonical_story_title

    ai_story["title"] = canonical_story_title
    ai_story["story_title"] = canonical_story_title
    ai_story["status"] = "completed"
    ai_story["total_parts"] = completed_parts
    ai_story["parts_count"] = completed_parts
    ai_story["total_scenes"] = completed_scenes
    ai_story["total_main_scenes"] = completed_scenes

    ai_story["actual_scene_counts"] = [
        len(part["scenes"]) for part in source_plan["parts"]
    ]

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
    log(
        "Last provider: "
        + str(written.get("last_successful_provider", "checkpoint"))
    )
    log(
        "Last model: "
        + str(written.get("last_successful_model", primary_model))
    )
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