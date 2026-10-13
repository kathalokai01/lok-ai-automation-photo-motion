#!/usr/bin/env python3
"""Prepare scene captions with validated caching and accurate API accounting."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "Input" / "topic.txt"
NARRATION_FILE = ROOT / "output" / "narration" / "narration.json"
MODEL_FILE = ROOT / "output" / "config" / "selected_model.json"
OUTPUT_FILE = ROOT / "output" / "captions" / "captions.json"

TIMEOUT = 180
API_VERSION = "v1beta"


def log(*items):
    print(*items, flush=True)


def stable_hash(value):
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def read_json(path, required=True):
    if not path.is_file():
        if required:
            raise RuntimeError(f"Required file not found: {path}")
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        if required:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
        log(f"WARNING: Ignoring invalid checkpoint {path}: {exc}")
        return None


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_config(path):
    if not path.is_file():
        raise RuntimeError(f"Input config not found: {path}")

    config = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip().upper()] = value.strip().strip("'\"")
    return config


def normalize_mode(value):
    aliases = {
        "hindi": "hindi",
        "hi": "hindi",
        "english": "english",
        "en": "english",
        "hinglish": "hinglish",
        "roman hindi": "hinglish",
        "roman-hindi": "hinglish",
    }
    mode = str(value or "hindi").strip().lower()
    if mode not in aliases:
        raise RuntimeError(
            f"Unsupported CAPTIONS mode {mode!r}; "
            "use hindi, english, or hinglish."
        )
    return aliases[mode]


def get_scene_records(narration):
    if not isinstance(narration, dict):
        raise RuntimeError("Narration JSON must be an object.")
    if narration.get("status") != "completed":
        raise RuntimeError("Narration is not marked completed.")

    scenes = narration.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("Narration contains no scenes.")

    records = []
    seen = set()

    for index, item in enumerate(scenes, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Invalid narration record at index {index}.")

        try:
            part = int(item["part"])
            scene = int(item.get("scene", item.get("local_scene")))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Narration record {index} needs numeric part and scene."
            ) from exc

        if part < 1 or scene < 1:
            raise RuntimeError("Part and scene numbers must be positive.")

        key = (part, scene)
        if key in seen:
            raise RuntimeError(f"Duplicate narration scene: {key}")
        seen.add(key)

        text = item.get("text", item.get("narration"))
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"Empty narration at Part {part}, Scene {scene}.")

        records.append({
            "part": part,
            "scene": scene,
            "source_text": text.strip(),
        })

    return sorted(records, key=lambda row: (row["part"], row["scene"]))


def load_selected_model():
    data = read_json(MODEL_FILE)
    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError("Gemini model selection is not marked selected.")

    model = str(data.get("model", "")).strip()
    if (
        not model
        or "/" in model
        or ":" in model
        or any(char.isspace() for char in model)
    ):
        raise RuntimeError(f"Invalid selected Gemini model name: {model!r}")
    return model


def transform_with_gemini(records, mode, model, api_key):
    if mode == "english":
        instruction = (
            "Translate each Hindi narration line into natural, faithful "
            "English. Do not summarize, add events, or omit meaning."
        )
    else:
        instruction = (
            "Transliterate each Hindi narration line into natural, readable "
            "Hinglish using Latin letters. Preserve Hindi words and meaning; "
            "do not translate into English or add content."
        )

    source = [
        {
            "id": index,
            "part": row["part"],
            "scene": row["scene"],
            "text": row["source_text"],
        }
        for index, row in enumerate(records, start=1)
    ]

    prompt = (
        "Prepare readable captions for a story video.\n"
        f"Task: {instruction}\n"
        'Return ONLY valid JSON: {"captions":[{"id":1,"text":"..."}]}\n'
        "Rules:\n"
        "- Include every input ID exactly once.\n"
        "- Preserve scene order and story meaning.\n"
        "- Keep captions concise and readable without losing key meaning.\n"
        "- No markdown or explanations.\n\n"
        "Input scenes:\n"
        + json.dumps(source, ensure_ascii=False)
    )

    endpoint = (
        f"https://generativelanguage.googleapis.com/{API_VERSION}/models/"
        f"{model}:generateContent"
    )

    try:
        response = requests.post(
            endpoint,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "contents": [{
                    "role": "user",
                    "parts": [{"text": prompt}],
                }],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                },
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Gemini caption request failed: {type(exc).__name__}: {exc}"
        ) from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Gemini caption request failed ({response.status_code}): "
            f"{response.text[:1000]}"
        )

    try:
        body = response.json()
        generated = body["candidates"][0]["content"]["parts"][0]["text"]
        captions = json.loads(generated)["captions"]
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise RuntimeError(f"Gemini returned invalid caption JSON: {exc}") from exc

    if not isinstance(captions, list):
        raise RuntimeError("Gemini captions field is not a list.")

    by_id = {}
    for item in captions:
        if not isinstance(item, dict):
            raise RuntimeError("Gemini returned an invalid caption record.")
        try:
            caption_id = int(item["id"])
            text = item["text"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Caption record lacks a valid ID or text.") from exc

        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Gemini returned an empty caption.")
        if caption_id in by_id:
            raise RuntimeError("Gemini returned duplicate caption IDs.")
        by_id[caption_id] = text.strip()

    expected_ids = set(range(1, len(records) + 1))
    if set(by_id) != expected_ids:
        raise RuntimeError("Gemini caption IDs do not match narration scene count.")

    return [by_id[index] for index in range(1, len(records) + 1)]


def reuse_existing_captions(mode, source_hash, records):
    existing = read_json(OUTPUT_FILE, required=False)
    if not isinstance(existing, dict):
        return None

    if (
        existing.get("status") != "completed"
        or existing.get("mode") != mode
        or existing.get("source_sha256") != source_hash
    ):
        return None

    captions = existing.get("captions")
    if not isinstance(captions, list) or len(captions) != len(records):
        return None

    expected_keys = [(row["part"], row["scene"]) for row in records]
    actual_keys = []
    texts = []

    for row in captions:
        if not isinstance(row, dict):
            return None
        try:
            key = (int(row["part"]), int(row["scene"]))
        except (KeyError, TypeError, ValueError):
            return None

        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            return None

        actual_keys.append(key)
        texts.append(text.strip())

    if actual_keys != expected_keys:
        return None
    return texts


def validate_and_save(records, output_texts, mode, source_hash, model, api_calls):
    if len(output_texts) != len(records):
        raise RuntimeError("Caption count does not match narration scene count.")

    output_records = []
    for row, caption_text in zip(records, output_texts):
        if not isinstance(caption_text, str) or not caption_text.strip():
            raise RuntimeError(
                f"Empty caption at Part {row['part']} Scene {row['scene']}."
            )
        output_records.append({
            "part": row["part"],
            "scene": row["scene"],
            "text": caption_text.strip(),
        })

    config = read_config(CONFIG_FILE)
    result = {
        "status": "completed",
        "mode": mode,
        "format": config.get("FORMAT", "short").strip().lower(),
        "source": str(NARRATION_FILE.relative_to(ROOT)),
        "source_sha256": source_hash,
        "model": model,
        "api_calls": int(api_calls),
        "total_scenes": len(output_records),
        "captions": output_records,
    }

    save_json(OUTPUT_FILE, result)
    saved = read_json(OUTPUT_FILE)

    if (
        not isinstance(saved, dict)
        or saved.get("status") != "completed"
        or saved.get("mode") != mode
        or saved.get("source_sha256") != source_hash
        or saved.get("api_calls") != int(api_calls)
        or len(saved.get("captions", [])) != len(records)
    ):
        raise RuntimeError("Saved captions failed validation.")

    log("Caption output validated:", OUTPUT_FILE.relative_to(ROOT))


def main():
    config = read_config(CONFIG_FILE)
    mode = normalize_mode(config.get("CAPTIONS", "hindi"))
    narration = read_json(NARRATION_FILE)
    records = get_scene_records(narration)

    source_hash = stable_hash([
        {
            "part": row["part"],
            "scene": row["scene"],
            "text": row["source_text"],
        }
        for row in records
    ])

    if mode == "hindi":
        log("Hindi captions: using original narration; Gemini calls: 0")
        validate_and_save(
            records,
            [row["source_text"] for row in records],
            mode,
            source_hash,
            None,
            0,
        )
        return

    cached = reuse_existing_captions(mode, source_hash, records)
    if cached is not None:
        old = read_json(OUTPUT_FILE)
        log(f"Reusing {len(cached)} validated {mode} captions; Gemini calls: 0")
        validate_and_save(
            records, cached, mode, source_hash, old.get("model"), 0
        )
        return

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Use CAPTIONS=hindi to avoid "
            "translation requests, or configure the Gemini API key."
        )

    model = load_selected_model()
    log(f"Generating {mode} captions with selected model: {model}")

    output_texts = transform_with_gemini(records, mode, model, api_key)
    validate_and_save(
        records, output_texts, mode, source_hash, model, 1
    )

    log("=" * 55)
    log("CAPTION PREPARATION: SUCCESS")
    log("Mode:", mode)
    log("Scenes:", len(output_texts))
    log("New Gemini requests: 1")
    log("Output:", OUTPUT_FILE.relative_to(ROOT))
    log("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CAPTION PREPARATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
