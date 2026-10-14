
#!/usr/bin/env python3
"""Generate resumable, source-aligned cinematic scenes with guarded API calls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from input_config import (
    load_input_config,
    normalize_format,
    get_parts,
    get_scenes,
    get_topic,
    get_story_text,
)
from gemini_api_guard import require_gemini_generation

ROOT = Path(__file__).resolve().parent.parent
STORY_DIR = ROOT / "output" / "story"
OUTPUT_DIR = ROOT / "output" / "scenes"
SCENES_FILE = OUTPUT_DIR / "scenes.json"
CHECKPOINT_FILE = ROOT / "output" / "checkpoints" / "scenes_progress.json"
RESPONSE_DIR = ROOT / "output" / "checkpoints" / "scene_responses"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"
BIBLE_FILE = STORY_DIR / "character_bible.json"

API_KEY = (
    os.getenv("GEMINI_API_KEY", "").strip()
    or os.getenv("GOOGLE_API_KEY", "").strip()
)
TIMEOUT = max(30, int(os.getenv("SCENE_REQUEST_TIMEOUT", "120")))
VERSION = 4


def log(message=""):
    print(message, flush=True)


def digest(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path, required=False):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def cfg_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def cfg_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    value = str(value).strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    return default


def word_count(value):
    return len(re.findall(r"\S+", str(value or "").strip()))


def selected_model():
    data = read_json(MODEL_FILE, required=True)
    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError(
            "No selected Gemini model. Run the model-selection step first."
        )
    model = str(data.get("model", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError("Invalid model ID in selected_model.json.")
    return model


def load_story():
    for path in (STORY_DIR / "ai_story.json", STORY_DIR / "story.json"):
        data = read_json(path)
        if isinstance(data, dict) and isinstance(data.get("parts"), list):
            if data["parts"]:
                log(f"Story source: {path}")
                return data
    raise RuntimeError("No usable story parts found.")


def load_bible(config):
    data = read_json(BIBLE_FILE)
    if data is None:
        if cfg_bool(config, "CHARACTER_BIBLE", True):
            raise RuntimeError(
                "Character Bible is enabled but character_bible.json is missing."
            )
        return {"characters": [], "world": {}}
    if not isinstance(data, dict):
        raise RuntimeError("character_bible.json must be a JSON object.")
    if data.get("status") == "disabled":
        return {"characters": [], "world": data.get("world", {})}
    if data.get("status") != "completed":
        raise RuntimeError("Character Bible is not marked completed.")
    return {
        "characters": data.get("characters", []),
        "world": data.get("world", {}),
    }


def build_settings(config):
    return {
        "format": normalize_format(config),
        "audience": cfg_text(config, "AUDIENCE", "adult"),
        "story_length": cfg_text(config, "STORY_LENGTH", "auto"),
        "scene_duration": cfg_text(config, "SCENE_DURATION", "auto"),
        "visual_style": cfg_text(config, "VISUAL_STYLE", "cinematic_realistic"),
        "realism": cfg_text(config, "REALISM", "high"),
        "quality": cfg_text(config, "QUALITY", "high"),
        "camera_style": cfg_text(config, "CAMERA_STYLE", "cinematic"),
        "lighting": cfg_text(config, "LIGHTING", "cinematic"),
        "mood": cfg_text(config, "MOOD", "dramatic"),
        "transitions": cfg_text(config, "TRANSITIONS", "cinematic"),
        "part_hook": cfg_bool(config, "PART_HOOK", True),
        "part_suspense": cfg_bool(config, "PART_SUSPENSE", True),
        "final_resolution": cfg_bool(config, "FINAL_RESOLUTION", True),
        "character_consistency": cfg_bool(config, "CHARACTER_CONSISTENCY", True),
        "world_consistency": cfg_bool(config, "WORLD_CONSISTENCY", True),
        "scene_continuity": cfg_bool(config, "SCENE_CONTINUITY", True),
        "natural_motion": cfg_bool(config, "NATURAL_MOTION", True),
        "music": cfg_bool(config, "MUSIC", True),
        "music_style": cfg_text(config, "MUSIC_STYLE", "cinematic"),
        "sfx": cfg_bool(config, "SFX", True),
        "ambient_sound": cfg_bool(config, "AMBIENT_SOUND", True),
    }


def validate_layout(story, parts_count, scenes_per_part):
    parts = story.get("parts")
    if not isinstance(parts, list) or len(parts) != parts_count:
        raise RuntimeError(
            f"Story parts mismatch: expected {parts_count}, "
            f"found {len(parts) if isinstance(parts, list) else 0}."
        )

    for p, part in enumerate(parts, 1):
        if not isinstance(part, dict):
            raise RuntimeError(f"Invalid part {p}.")
        try:
            part_number = int(part.get("part", p))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid part number at part {p}.") from exc
        if part_number != p:
            raise RuntimeError(f"Invalid part numbering at part {p}.")

        source_scenes = part.get("scenes")
        if (
            not isinstance(source_scenes, list)
            or len(source_scenes) != scenes_per_part
        ):
            raise RuntimeError(
                f"Part {p}: expected {scenes_per_part} source scenes, "
                f"found {len(source_scenes) if isinstance(source_scenes, list) else 0}."
            )
        for s, source in enumerate(source_scenes, 1):
            if not isinstance(source, dict):
                raise RuntimeError(f"Invalid source scene {p}/{s}.")
            try:
                scene_number = int(source.get("scene", s))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid scene number at {p}/{s}."
                ) from exc
            if scene_number != s:
                raise RuntimeError(
                    f"Invalid scene numbering in part {p}, scene {s}."
                )


def scene_key(part, scene):
    return f"{part}:{scene}"


def stable_scene_id(part, scene):
    return f"part_{part:02d}_scene_{scene:02d}"


def source_scene(story, part, scene):
    return story["parts"][part - 1]["scenes"][scene - 1]


def needs_hook(part, scene, settings):
    return scene == 1 and (part == 1 or settings["part_hook"])


def needs_suspense(scene, scene_count, settings):
    return scene == scene_count and settings["part_suspense"]


def validate_scene(item, part, scene, scene_count, settings):
    if not isinstance(item, dict):
        raise RuntimeError("Generated scene is not a JSON object.")

    try:
        got_part = int(item.get("part", part))
        got_scene = int(item.get("scene", scene))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Generated scene has invalid numbering.") from exc

    if got_part != part or got_scene != scene:
        raise RuntimeError(
            f"Generated part/scene mismatch: expected {part}/{scene}."
        )

    fields = (
        "title", "narration", "visual_prompt", "negative_prompt",
        "duration", "transition", "camera", "lighting", "mood",
        "sfx", "ambient_sound", "music_direction", "world_context",
        "suspense_prompt",
    )
    item = dict(item)
    for field in fields:
        value = item.get(field, "")
        if not isinstance(value, str):
            raise RuntimeError(
                f"{stable_scene_id(part, scene)}: {field} must be text."
            )
        item[field] = value.strip()

    if not item["narration"]:
        raise RuntimeError(f"{stable_scene_id(part, scene)} has empty narration.")
    if len(item["visual_prompt"]) < 30:
        raise RuntimeError(
            f"{stable_scene_id(part, scene)} has a short visual prompt."
        )

    if needs_hook(part, scene, settings):
        minimum = 7 if settings["format"] == "short" else 14
        if word_count(item["narration"]) < minimum:
            raise RuntimeError(
                f"{stable_scene_id(part, scene)} hook needs at least {minimum} words."
            )

    if needs_suspense(scene, scene_count, settings):
        if word_count(item["suspense_prompt"]) < 4:
            raise RuntimeError(
                f"{stable_scene_id(part, scene)} needs a specific suspense prompt."
            )

    item.update({
        "part": part,
        "scene": scene,
        "part_id": f"part_{part:02d}",
        "scene_id": stable_scene_id(part, scene),
        "global_scene_number": (part - 1) * scene_count + scene,
        "mapping_version": VERSION,
        "status": "completed",
        "opening_hook_required": needs_hook(part, scene, settings),
        "suspense_required": needs_suspense(scene, scene_count, settings),
    })

    if not settings["sfx"]:
        item["sfx"] = ""
    if not settings["music"]:
        item["music_direction"] = ""
    if not settings["ambient_sound"]:
        item["ambient_sound"] = ""

    return item


def scene_valid(item, part, scene, scene_count, settings):
    if not isinstance(item, dict) or item.get("status") != "completed":
        return False
    try:
        checked = validate_scene(item, part, scene, scene_count, settings)
        return (
            checked.get("scene_id") == stable_scene_id(part, scene)
            and checked.get("global_scene_number")
            == (part - 1) * scene_count + scene
        )
    except (RuntimeError, TypeError, ValueError):
        return False


def call_gemini(model, prompt):
    if not API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY or GOOGLE_API_KEY secret is missing."
        )

    # Billing guard is deliberately immediately before the network request.
    # Existing validated scenes and saved responses do not need this call.
    require_gemini_generation("scene generation", api_key=API_KEY)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + urllib.parse.quote(model, safe="-._")
        + ":generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.65,
            "topP": 0.9,
            "responseMimeType": "application/json",
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": API_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            "Gemini request timed out or had a network error. Its outcome "
            f"may be uncertain; automatic retry stopped to avoid duplicate calls: {exc}"
        ) from exc

    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(
            str(data.get("error", {}).get(
                "message", "No Gemini candidate returned."
            ))
        )

    candidate = candidates[0]
    if str(candidate.get("finishReason", "")).upper() in {
        "MAX_TOKENS", "LENGTH"
    }:
        raise RuntimeError("Gemini response was truncated.")

    text = "\n".join(
        p.get("text", "")
        for p in candidate.get("content", {}).get("parts", [])
        if isinstance(p, dict) and isinstance(p.get("text"), str)
    ).strip()
    if not text:
        raise RuntimeError("Gemini returned empty text.")

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini did not return valid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Gemini scene must be a JSON object.")
    return result


def make_prompt(
    story, bible, source_part, source, part, scene, scene_count,
    settings, topic, story_text, previous,
):
    hook_required = needs_hook(part, scene, settings)
    suspense_required = needs_suspense(scene, scene_count, settings)
    final_scene = (
        part == len(story["parts"])
        and scene == scene_count
        and settings["final_resolution"]
    )

    hook_rule = (
        "Opening scene: create a strong Hindi curiosity hook immediately."
        if hook_required else
        "Continue naturally from the exact source event."
    )
    suspense_rule = (
        "This is the part-ending scene. Include a specific suspense beat "
        "in narration and suspense_prompt."
        if suspense_required else
        "Do not force a cliffhanger if the source does not call for one."
    )
    ending_rule = (
        "Final story scene: resolve the central conflict clearly."
        if final_scene else
        "Do not prematurely resolve the overall story."
    )

    return f"""
You are the Hindi cinematic scene director for Katha Lok AI.
Return only one valid JSON object. No Markdown.

TOPIC: {topic}
USER STORY TEXT: {story_text or "(not supplied)"}
PART: {part}/{len(story["parts"])}
SCENE: {scene}/{scene_count}

EXACT SOURCE SCENE:
{json.dumps(source, ensure_ascii=False, indent=2)}

SOURCE PART:
{json.dumps(source_part, ensure_ascii=False)}

CHARACTER AND WORLD BIBLE:
{json.dumps(bible, ensure_ascii=False)}

PREVIOUS SCENES:
{json.dumps(previous, ensure_ascii=False)}

FORMAT: {settings["format"]}
AUDIENCE: {settings["audience"]}
STORY LENGTH: {settings["story_length"]}
SCENE DURATION: {settings["scene_duration"]}
VISUAL STYLE: {settings["visual_style"]}
REALISM: {settings["realism"]}
QUALITY: {settings["quality"]}
CAMERA: {settings["camera_style"]}
LIGHTING: {settings["lighting"]}
MOOD: {settings["mood"]}
TRANSITIONS: {settings["transitions"]}

HOOK REQUIRED: {hook_required}
{hook_rule}
First short hook: at least 7 words. First full hook: at least 14 words.
No greeting, channel introduction, or subscribe request.

SUSPENSE REQUIRED: {suspense_required}
{suspense_rule}

ENDING:
{ending_rule}

STRICT STORY ALIGNMENT:
- Generate exactly this source scene; never add, omit or merge events.
- Preserve names, relationships, ages, clothing, locations and timeline.
- Keep narration in natural Hindi and preserve the source event's meaning.
- Maintain consistent character identity and wardrobe.
- Describe one coherent, photorealistic live-action image moment.
- No cartoons, anime, comics, collages or slideshow aesthetics.
- Include believable camera framing, lighting, actions and expressions.
- Avoid face distortion, extra fingers, duplicated people and glitches.
- Do not invent unrelated events.

Return these fields as strings:
{{
  "part": {part},
  "scene": {scene},
  "title": "short title",
  "narration": "Hindi narration",
  "visual_prompt": "detailed photorealistic live-action image prompt",
  "negative_prompt": "unwanted visual elements",
  "duration": "{settings["scene_duration"]}",
  "transition": "{settings["transitions"]}",
  "camera": "{settings["camera_style"]}",
  "lighting": "{settings["lighting"]}",
  "mood": "{settings["mood"]}",
  "sfx": "",
  "ambient_sound": "",
  "music_direction": "",
  "world_context": "location and continuity details",
  "suspense_prompt": "specific suspense or empty string"
}}
""".strip()


def response_checkpoint_path(source_fp, part, scene):
    return RESPONSE_DIR / source_fp / f"{stable_scene_id(part, scene)}.json"


def save_outputs(
    scenes, source_fp, settings, model, topic, story_text,
    status, total, failed_scene=None,
):
    ordered = sorted(
        scenes.values(),
        key=lambda item: (int(item["part"]), int(item["scene"])),
    )
    completed_ids = [item["scene_id"] for item in ordered]

    write_json(SCENES_FILE, {
        "status": status,
        "mapping_version": VERSION,
        "scene_order": "part_then_scene",
        "source_fingerprint": source_fp,
        "model": model,
        "topic": topic,
        "story_text": story_text,
        "input_config": settings,
        "total_scenes": total,
        "completed_scenes": len(ordered),
        "scenes": ordered,
    })
    write_json(CHECKPOINT_FILE, {
        "status": status,
        "mapping_version": VERSION,
        "source_fingerprint": source_fp,
        "model": model,
        "total_scenes": total,
        "completed_count": len(ordered),
        "completed_scene_ids": completed_ids,
        "failed_scene": failed_scene,
        "updated_at": int(time.time()),
    })


def main():
    config = load_input_config()
    settings = build_settings(config)
    parts = int(get_parts(config))
    scene_count = int(get_scenes(config))
    if parts < 1 or scene_count < 1:
        raise RuntimeError("PARTS and SCENES must be positive integers.")

    story = load_story()
    validate_layout(story, parts, scene_count)
    bible = load_bible(config)
    model = selected_model()
    topic = str(get_topic(config) or story.get("topic", "")).strip()
    story_text = str(
        get_story_text(config) or story.get("story_text", "")
    ).strip()

    if not topic and not story_text:
        raise RuntimeError("TOPIC and STORY_TEXT are both empty.")

    hook = str(story.get("hook", "")).strip()
    if not hook or word_count(hook) < (
        7 if settings["format"] == "short" else 14
    ):
        raise RuntimeError("The source story hook is missing or too short.")

    source_fp = digest({
        "mapping_version": VERSION,
        "story": story,
        "bible": bible,
        "model": model,
        "settings": settings,
        "topic": topic,
        "story_text": story_text,
        "parts": parts,
        "scenes_per_part": scene_count,
    })
    total = parts * scene_count

    saved = read_json(SCENES_FILE)
    scenes = {}

    if (
        isinstance(saved, dict)
        and saved.get("source_fingerprint") == source_fp
        and saved.get("mapping_version") == VERSION
        and isinstance(saved.get("scenes"), list)
    ):
        for item in saved["scenes"]:
            if not isinstance(item, dict):
                continue
            try:
                p, s = int(item["part"]), int(item["scene"])
            except (KeyError, TypeError, ValueError):
                continue
            if 1 <= p <= parts and 1 <= s <= scene_count:
                if scene_valid(item, p, s, scene_count, settings):
                    scenes[scene_key(p, s)] = item

    log(f"Format: {settings['format']}")
    log(f"Parts: {parts}; scenes per part: {scene_count}; total: {total}")
    log(f"Model: {model}")
    log(f"Validated scene checkpoints reused: {len(scenes)}/{total}")

    for p in range(1, parts + 1):
        source_part = story["parts"][p - 1]

        for s in range(1, scene_count + 1):
            key = scene_key(p, s)
            if key in scenes:
                log(f"{stable_scene_id(p, s)}: completed scene reused.")
                continue

            source = source_scene(story, p, s)
            previous = sorted(
                [
                    item for item in scenes.values()
                    if (int(item["part"]), int(item["scene"])) < (p, s)
                ],
                key=lambda item: (int(item["part"]), int(item["scene"])),
            )[-3:]

            checkpoint_path = response_checkpoint_path(source_fp, p, s)
            checkpoint = read_json(checkpoint_path)
            response = None

            if (
                isinstance(checkpoint, dict)
                and checkpoint.get("source_fingerprint") == source_fp
                and checkpoint.get("scene_id") == stable_scene_id(p, s)
                and isinstance(checkpoint.get("response"), dict)
            ):
                response = checkpoint["response"]
                log(f"{stable_scene_id(p, s)}: raw response checkpoint reused.")

            if response is None:
                prompt = make_prompt(
                    story, bible, source_part, source, p, s,
                    scene_count, settings, topic, story_text, previous,
                )

                try:
                    response = call_gemini(model, prompt)
                except Exception as exc:
                    save_outputs(
                        scenes, source_fp, settings, model, topic, story_text,
                        "in_progress", total, failed_scene=key,
                    )
                    raise RuntimeError(
                        f"{stable_scene_id(p, s)} request stopped: {exc}. "
                        "No automatic retry was made. Check provider usage "
                        "before manually retrying an uncertain request."
                    ) from exc

                # Save the response before validating it, avoiding a second
                # generation request if the schema needs later correction.
                write_json(checkpoint_path, {
                    "status": "response_saved",
                    "mapping_version": VERSION,
                    "source_fingerprint": source_fp,
                    "scene_id": stable_scene_id(p, s),
                    "part": p,
                    "scene": s,
                    "model": model,
                    "response": response,
                })
                log(f"{stable_scene_id(p, s)}: raw response checkpoint saved.")

            try:
                item = validate_scene(
                    response, p, s, scene_count, settings
                )
            except (RuntimeError, TypeError, ValueError, KeyError) as exc:
                save_outputs(
                    scenes, source_fp, settings, model, topic, story_text,
                    "in_progress", total, failed_scene=key,
                )
                raise RuntimeError(
                    f"{stable_scene_id(p, s)} response failed validation: {exc}. "
                    f"Raw response retained at {checkpoint_path}. "
                    "No second generation request was made."
                ) from exc

            item["source_part"] = p
            item["source_scene"] = s
            item["source_scene_id"] = source.get(
                "scene_id", stable_scene_id(p, s)
            )
            scenes[key] = item

            save_outputs(
                scenes, source_fp, settings, model, topic, story_text,
                "in_progress", total,
            )
            log(
                f"Checkpoint saved: {stable_scene_id(p, s)} "
                f"({len(scenes)}/{total})."
            )

    expected = {
        scene_key(p, s)
        for p in range(1, parts + 1)
        for s in range(1, scene_count + 1)
    }
    if set(scenes) != expected:
        raise RuntimeError("Final scene count or mapping is incomplete.")

    ordered = sorted(
        scenes.values(),
        key=lambda item: (int(item["part"]), int(item["scene"])),
    )
    expected_ids = [
        stable_scene_id(p, s)
        for p in range(1, parts + 1)
        for s in range(1, scene_count + 1)
    ]
    if [item["scene_id"] for item in ordered] != expected_ids:
        raise RuntimeError("Stable scene ID sequence validation failed.")

    save_outputs(
        scenes, source_fp, settings, model, topic, story_text,
        "completed", total,
    )
    log("SCENE GENERATION: SUCCESS")
    log(f"Output: {SCENES_FILE}")
    log(f"Progress checkpoint: {CHECKPOINT_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
