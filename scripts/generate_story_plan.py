
#!/usr/bin/env python3
"""Generate resumable AI story scenes with per-part response checkpoints."""

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
    get_max_retries,
    get_topic,
    get_story_text,
)

ROOT = Path(__file__).resolve().parent.parent
STORY_DIR = ROOT / "output" / "story"
INPUT = STORY_DIR / "story.json"
OUTPUT = STORY_DIR / "ai_story.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"
CHECKPOINT_DIR = STORY_DIR / "ai_story_checkpoints"

API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TIMEOUT = max(30, int(os.getenv("SCENE_REQUEST_TIMEOUT", "120")))
VERSION = 3

SCENE_FIELDS = (
    "narration",
    "dialogue",
    "visual_prompt",
    "negative_prompt",
    "camera_prompt",
    "lighting_prompt",
    "sfx_prompt",
    "music_prompt",
    "suspense_prompt",
)


def log(message):
    print(message, flush=True)


def fingerprint(data):
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file missing: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON at {path}: {exc}") from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def normalize_text(value):
    value = str(value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def word_count(value):
    return len(normalize_text(value).split())


def get_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


def get_text(config, key, default=""):
    value = config.get(key, default)
    return str(default if value is None else value).strip()


def selected_model():
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError(
            "Selected Gemini model missing. Run "
            "scripts/select_gemini_model.py first."
        )
    model = str(data.get("model", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise RuntimeError(f"Invalid Gemini model ID: {model!r}")
    return model


def build_config(source):
    try:
        retries = int(get_max_retries(source))
    except (TypeError, ValueError):
        retries = 1

    return {
        "format": normalize_format(source),
        "audience": get_text(source, "AUDIENCE", "adult"),
        "story_length": get_text(source, "STORY_LENGTH", "auto"),
        "scene_duration": get_text(source, "SCENE_DURATION", "auto"),
        "part_hook": get_bool(source, "PART_HOOK", True),
        "part_suspense": get_bool(source, "PART_SUSPENSE", True),
        "final_resolution": get_bool(source, "FINAL_RESOLUTION", True),
        "visual_style": get_text(
            source, "VISUAL_STYLE", "cinematic_realistic"
        ),
        "realism": get_text(source, "REALISM", "high"),
        "camera_style": get_text(source, "CAMERA_STYLE", "cinematic"),
        "lighting": get_text(source, "LIGHTING", "cinematic"),
        "mood": get_text(source, "MOOD", "dramatic"),
        "music": get_bool(source, "MUSIC", True),
        "music_style": get_text(source, "MUSIC_STYLE", "cinematic"),
        "sfx": get_bool(source, "SFX", True),
        "ambient_sound": get_bool(source, "AMBIENT_SOUND", True),
        "max_retries": max(1, min(3, retries)),
    }


def validate_source(story, expected_parts, expected_scenes):
    parts = story.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("story.json must contain a non-empty parts list.")
    if len(parts) != expected_parts:
        raise RuntimeError(
            f"Story parts mismatch: got {len(parts)}, "
            f"expected {expected_parts}."
        )

    total = 0
    for pnum, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("part") != pnum:
            raise RuntimeError(f"Invalid part numbering at part {pnum}.")
        scenes = part.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != expected_scenes:
            raise RuntimeError(
                f"Part {pnum} must contain exactly {expected_scenes} scenes."
            )
        for snum, scene in enumerate(scenes, 1):
            if not isinstance(scene, dict) or scene.get("scene") != snum:
                raise RuntimeError(
                    f"Invalid scene numbering at part {pnum}, scene {snum}."
                )
        total += len(scenes)
    return total


def call_gemini(model, prompt):
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret is missing.")

    encoded_model = urllib.parse.quote(model, safe="-._")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{encoded_model}:generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.65,
            "topP": 0.9,
            "maxOutputTokens": 16000,
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
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Gemini network error; request may have reached provider: {exc}"
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError(
            "Gemini request timed out; provider may have processed it."
        ) from exc

    candidates = result.get("candidates", [])
    if not candidates:
        error = result.get("error", {})
        raise RuntimeError(
            str(error.get("message", "Gemini returned no candidates."))
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
        raise RuntimeError("Gemini returned empty response text.")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini returned invalid JSON.") from exc

    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
        raise RuntimeError("Gemini response must contain a scenes array.")
    return data


def make_prompt(story, part, config, topic, story_text):
    pnum = int(part["part"])
    sources = part["scenes"]
    first_part = pnum == 1
    last_part = pnum == len(story["parts"])
    source_context = []

    for scene in sources:
        source_context.append({
            "scene": scene["scene"],
            "scene_id": scene.get("scene_id", ""),
            "global_scene_number": scene.get("global_scene_number"),
            "purpose": scene.get("purpose", scene.get("role", "")),
            "narration": scene.get("narration", ""),
            "visual_prompt": scene.get(
                "visual_prompt", scene.get("visual", "")
            ),
            "dialogue": scene.get("dialogue", ""),
            "suspense_prompt": scene.get("suspense_prompt", ""),
        })

    if first_part:
        hook_rule = (
            "Scene 1 narration must begin with the exact story hook below."
        )
    elif config["part_hook"]:
        hook_rule = "Scene 1 must have a distinct, natural part-opening hook."
    else:
        hook_rule = "Continue naturally from the previous part."

    suspense_rule = (
        "The final scene needs a story-specific suspense_prompt with "
        "at least four words."
        if config["part_suspense"]
        else "Use suspense only when it fits the source story."
    )
    ending_rule = (
        "This is the final part: resolve the central conflict clearly."
        if last_part and config["final_resolution"]
        else "Preserve continuity and lead naturally into the next part."
    )

    return f"""
You are the Hindi cinematic story writer for Katha Lok AI.
Return only valid JSON. No Markdown or extra commentary.

TOPIC: {topic}
USER STORY TEXT: {story_text or "(not supplied)"}
TITLE: {story.get("title", topic)}
EXACT STORY HOOK: {story.get("hook", "")}
FORMAT: {config["format"]}
AUDIENCE: {config["audience"]}
STORY LENGTH: {config["story_length"]}
SCENE DURATION: {config["scene_duration"]}
PART: {pnum} of {len(story["parts"])}

SOURCE SCENES:
{json.dumps(source_context, ensure_ascii=False, indent=2)}

HOOK RULE:
{hook_rule}
For the first short-format hook use at least 7 words.
For the first full-format hook use at least 14 words.
No greetings, channel introductions, or subscribe requests.

ENDING RULES:
{suspense_rule}
{ending_rule}

CONTINUITY:
- Return exactly {len(sources)} scenes, in source order.
- Never merge, omit, duplicate, or add scenes.
- Preserve characters, names, ages, relationships, clothing, and timeline.
- Narration must be natural Hindi and dialogue believable.
- Every scene must match its source event.
- Photorealistic live-action only; no cartoon or slideshow look.
- Describe plausible actions, lighting, framing, and camera movement.
- Use empty strings for optional dialogue/audio prompts when unnecessary.

STYLE SETTINGS:
visual_style={config["visual_style"]}
realism={config["realism"]}
camera={config["camera_style"]}
lighting={config["lighting"]}
mood={config["mood"]}
music_enabled={config["music"]}
music_style={config["music_style"]}
sfx_enabled={config["sfx"]}
ambient_sound_enabled={config["ambient_sound"]}

Every scene needs these string fields:
narration, dialogue, visual_prompt, negative_prompt, camera_prompt,
lighting_prompt, sfx_prompt, music_prompt, suspense_prompt.
The scene field must be its integer scene number.

Required JSON shape:
{{"scenes":[{{"scene":1,"narration":"...","dialogue":"",
"visual_prompt":"...","negative_prompt":"","camera_prompt":"",
"lighting_prompt":"","sfx_prompt":"","music_prompt":"",
"suspense_prompt":""}}]}}
""".strip()


def validate_generated(generated, source_part, config, hook):
    source_scenes = source_part["scenes"]
    pnum = int(source_part["part"])

    if not isinstance(generated, list) or len(generated) != len(source_scenes):
        count = len(generated) if isinstance(generated, list) else "invalid"
        raise RuntimeError(
            f"Part {pnum}: expected {len(source_scenes)} scenes, got {count}."
        )

    output = []
    for index, (source, scene) in enumerate(
        zip(source_scenes, generated), 1
    ):
        if not isinstance(scene, dict) or scene.get("scene") != index:
            raise RuntimeError(f"Part {pnum}: scene {index} mapping mismatch.")

        item = {
            "part": pnum,
            "scene": index,
            "part_id": f"part_{pnum:02d}",
            "scene_id": f"part_{pnum:02d}_scene_{index:02d}",
            "global_scene_number": source.get("global_scene_number"),
            "mapping_version": VERSION,
            "source_scene_id": source.get(
                "scene_id", f"part_{pnum:02d}_scene_{index:02d}"
            ),
            "role": str(source.get("role", source.get("purpose", ""))),
            "status": "completed",
        }

        for field in SCENE_FIELDS:
            value = scene.get(field, "")
            if not isinstance(value, str):
                raise RuntimeError(
                    f"Part {pnum}, scene {index}: {field} must be text."
                )
            item[field] = value.strip()

        if not item["narration"]:
            raise RuntimeError(f"Part {pnum}, scene {index}: empty narration.")
        if len(item["visual_prompt"]) < 30:
            raise RuntimeError(
                f"Part {pnum}, scene {index}: visual prompt too short."
            )

        if index == 1 and (pnum == 1 or config["part_hook"]):
            minimum = (
                7 if config["format"] == "short" else 14
            ) if pnum == 1 else 7
            if word_count(item["narration"]) < minimum:
                raise RuntimeError(
                    f"Part {pnum}, scene 1 hook needs at least {minimum} words."
                )

        if pnum == 1 and index == 1 and hook:
            if not normalize_text(item["narration"]).startswith(
                normalize_text(hook)
            ):
                raise RuntimeError("First narration does not start with exact hook.")

        if config["part_suspense"] and index == len(source_scenes):
            if word_count(item["suspense_prompt"]) < 4:
                raise RuntimeError(
                    f"Part {pnum}, final scene needs a specific suspense prompt."
                )

        if not config["sfx"]:
            item["sfx_prompt"] = ""
        if not config["music"]:
            item["music_prompt"] = ""

        output.append(item)

    return output


def part_checkpoint_path(part_number):
    return CHECKPOINT_DIR / f"part_{part_number:02d}.json"


def main():
    source_config = load_input_config()
    config = build_config(source_config)
    model = selected_model()
    story = read_json(INPUT)

    if not isinstance(story, dict) or story.get("status") != "completed":
        raise RuntimeError("story.json is missing or not marked completed.")

    parts_requested = int(source_config.get("PARTS", len(story.get("parts", []))))
    scenes_requested = int(source_config.get("SCENES", 0))
    if parts_requested < 1 or scenes_requested < 1:
        raise RuntimeError("PARTS and SCENES must both be positive integers.")

    total = validate_source(story, parts_requested, scenes_requested)
    topic = str(get_topic(source_config) or story.get("topic", "")).strip()
    story_text = str(get_story_text(source_config) or "").strip()

    if not topic and not story_text:
        raise RuntimeError("TOPIC and STORY_TEXT are both empty.")

    hook = str(story.get("hook", "")).strip()
    minimum_hook = 7 if config["format"] == "short" else 14
    if word_count(hook) < minimum_hook:
        raise RuntimeError(
            f"story.json hook needs at least {minimum_hook} words."
        )

    source_fp = fingerprint({
        "mapping_version": VERSION,
        "story": story,
        "topic": topic,
        "story_text": story_text,
        "config": config,
        "model": model,
    })

    existing = read_json(OUTPUT, required=False)
    saved_parts = {}
    if (
        isinstance(existing, dict)
        and existing.get("source_fingerprint") == source_fp
        and existing.get("mapping_version") == VERSION
        and isinstance(existing.get("parts"), list)
    ):
        for part in existing["parts"]:
            if isinstance(part, dict):
                try:
                    saved_parts[int(part["part"])] = part
                except (KeyError, TypeError, ValueError):
                    pass

    result = {
        "status": "in_progress",
        "mapping_version": VERSION,
        "scene_order": "part_then_scene",
        "topic": topic,
        "story_text": story_text,
        "format": config["format"],
        "model": model,
        "source_fingerprint": source_fp,
        "total_scenes": total,
        "parts": [],
    }

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Model: {model}")
    log(f"Format: {config['format']}")
    log(f"Parts: {parts_requested}; scenes per part: {scenes_requested}")
    log(f"Total scenes: {total}")
    log("A valid raw response checkpoint is reused before another API call.")

    global_number = 0

    for source_part in story["parts"]:
        pnum = int(source_part["part"])
        part_fp = fingerprint({
            "source_fingerprint": source_fp,
            "part": source_part,
        })
        saved = saved_parts.get(pnum)
        scenes = None

        if (
            isinstance(saved, dict)
            and saved.get("status") == "completed"
            and saved.get("part_fingerprint") == part_fp
            and isinstance(saved.get("scenes"), list)
        ):
            try:
                scenes = validate_generated(
                    saved["scenes"], source_part, config, hook
                )
                log(f"Part {pnum}: validated completed checkpoint reused.")
            except (RuntimeError, TypeError, ValueError, KeyError):
                scenes = None

        if scenes is None:
            checkpoint_path = part_checkpoint_path(pnum)
            raw_checkpoint = read_json(checkpoint_path, required=False)
            response = None

            if (
                isinstance(raw_checkpoint, dict)
                and raw_checkpoint.get("part_fingerprint") == part_fp
                and isinstance(raw_checkpoint.get("response"), dict)
                and isinstance(raw_checkpoint["response"].get("scenes"), list)
            ):
                response = raw_checkpoint["response"]
                log(f"Part {pnum}: reusing saved raw Gemini response.")

            if response is None:
                if not API_KEY:
                    raise RuntimeError("GEMINI_API_KEY secret is missing.")

                last_error = None
                max_attempts = config["max_retries"]
                for attempt in range(1, max_attempts + 1):
                    log(f"Part {pnum}: Gemini request {attempt}/{max_attempts}.")
                    try:
                        response = call_gemini(
                            model,
                            make_prompt(
                                story, source_part, config, topic, story_text
                            ),
                        )
                        # Save raw successful response before schema validation.
                        write_json(checkpoint_path, {
                            "status": "response_saved",
                            "mapping_version": VERSION,
                            "part": pnum,
                            "part_fingerprint": part_fp,
                            "model": model,
                            "response": response,
                        })
                        break
                    except RuntimeError as exc:
                        last_error = exc
                        log(f"Part {pnum}: {exc}")

                        # A timeout/network failure may still have consumed
                        # provider quota. Do not automatically retry it.
                        message = str(exc).lower()
                        uncertain = any(word in message for word in (
                            "timed out", "network error", "urlopen error"
                        ))
                        if uncertain:
                            raise RuntimeError(
                                f"Part {pnum}: request outcome is uncertain; "
                                "stopping to avoid duplicate API charges. "
                                "If a response was received, rerun to reuse its "
                                "checkpoint; otherwise inspect quota before retrying."
                            ) from exc

                        if attempt < max_attempts:
                            time.sleep(min(2 ** attempt, 4))

                if response is None:
                    write_json(OUTPUT, result)
                    raise RuntimeError(
                        f"Part {pnum} failed: {last_error}"
                    )

            try:
                scenes = validate_generated(
                    response.get("scenes", []),
                    source_part,
                    config,
                    hook,
                )
            except (RuntimeError, TypeError, ValueError, KeyError) as exc:
                # Keep the raw response. A corrected script can revalidate it
                # without generating the story again.
                write_json(OUTPUT, result)
                raise RuntimeError(
                    f"Part {pnum} response failed validation. Raw response "
                    f"saved at {checkpoint_path}; no automatic second "
                    f"generation request was made. Details: {exc}"
                ) from exc

        for scene in scenes:
            global_number += 1
            source_global = scene.get("global_scene_number")
            if not isinstance(source_global, int):
                scene["global_scene_number"] = global_number

        part_result = {
            "part": pnum,
            "part_id": f"part_{pnum:02d}",
            "status": "completed",
            "part_fingerprint": part_fp,
            "title": str(source_part.get("title") or f"Part {pnum}"),
            "hook_required": pnum == 1 or config["part_hook"],
            "suspense_required": config["part_suspense"],
            "final_resolution": (
                config["final_resolution"]
                and pnum == len(story["parts"])
            ),
            "scenes": scenes,
        }
        result["parts"].append(part_result)
        write_json(OUTPUT, result)
        log(
            f"Part {pnum} checkpointed: "
            f"{sum(len(p['scenes']) for p in result['parts'])}/{total} scenes."
        )

    actual = sum(len(part["scenes"]) for part in result["parts"])
    if actual != total:
        raise RuntimeError(f"Final scene count mismatch: {actual}/{total}.")

    expected_ids = [
        f"part_{p:02d}_scene_{s:02d}"
        for p, part in enumerate(result["parts"], 1)
        for s in range(1, len(part["scenes"]) + 1)
    ]
    actual_ids = [
        scene["scene_id"]
        for part in result["parts"]
        for scene in part["scenes"]
    ]
    if actual_ids != expected_ids:
        raise RuntimeError("Stable scene ID sequence validation failed.")

    result["status"] = "completed"
    result["validation"] = {
        "mapping_version": VERSION,
        "total_scenes": actual,
        "stable_scene_ids": True,
        "sequential_scene_mapping": True,
        "opening_hook_validated": True,
        "part_suspense_validated": config["part_suspense"],
    }
    write_json(OUTPUT, result)

    log("AI STORY GENERATION: SUCCESS")
    log(f"Output: {OUTPUT}")
    log(f"Validated scenes: {actual}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
