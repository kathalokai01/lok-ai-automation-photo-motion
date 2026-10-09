#!/usr/bin/env python3
"""Prepare Hindi, English, or Hinglish captions for each narration scene."""

import json
import os
import re
import sys
from pathlib import Path

import requests

CONFIG_FILE = Path("Input/topic.txt")
NARRATION_FILE = Path("output/narration/narration.json")
MODEL_FILE = Path("output/config/selected_model.json")
OUTPUT_FILE = Path("output/captions/captions.json")


def read_config(path):
    if not path.is_file():
        raise RuntimeError(f"Input config not found: {path}")

    config = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        value = value.split("#", 1)[0].strip().strip("'\"")
        config[key.strip().upper()] = value

    return config


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required file not found: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def normalize_mode(value):
    value = str(value or "hindi").strip().lower()
    aliases = {
        "hindi": "hindi",
        "hi": "hindi",
        "english": "english",
        "en": "english",
        "hinglish": "hinglish",
        "roman hindi": "hinglish",
        "roman-hindi": "hinglish",
    }

    if value not in aliases:
        raise RuntimeError(
            f"Unsupported CAPTIONS mode: {value!r}. "
            "Use hindi, english, or hinglish."
        )

    return aliases[value]


def get_scene_texts(narration):
    scenes = narration.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("Narration contains no scenes.")

    records = []
    seen = set()

    for item in scenes:
        if not isinstance(item, dict):
            raise RuntimeError("Narration contains an invalid scene record.")

        try:
            part = int(item["part"])
            scene = int(item["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Every narration scene must have numeric part and scene fields."
            ) from exc

        text = str(item.get("text", "")).strip()
        if not text:
            raise RuntimeError(f"Empty narration: Part {part}, Scene {scene}")

        key = (part, scene)
        if key in seen:
            raise RuntimeError(f"Duplicate narration scene: Part {part}, Scene {scene}")

        seen.add(key)
        records.append({
            "part": part,
            "scene": scene,
            "source_text": text,
        })

    return sorted(records, key=lambda row: (row["part"], row["scene"]))


def load_selected_model():
    data = read_json(MODEL_FILE)

    if data.get("status") != "selected":
        raise RuntimeError("Gemini model selection is not marked as selected.")

    model = str(data.get("model", "")).strip()
    if not model:
        raise RuntimeError("Selected model file has no model name.")

    return model


def transform_with_gemini(records, mode, model, api_key):
    if mode == "english":
        instruction = (
            "Translate each Hindi narration line into natural, faithful English. "
            "Do not summarize, add events, or omit meaning. Return English text."
        )
    else:
        instruction = (
            "Transliterate each Hindi narration line into natural, readable "
            "Hinglish using Latin letters. Keep the same Hindi words and meaning; "
            "do NOT translate them into English. Do not summarize or add content."
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
        "You are preparing subtitles for a Hindi story video.\n"
        f"Task: {instruction}\n"
        "Return ONLY valid JSON in this exact shape:\n"
        '{"captions":[{"id":1,"text":"..."}]}\n'
        "Rules:\n"
        "- Include every input id exactly once.\n"
        "- Preserve the original story meaning and scene order.\n"
        "- Keep each caption concise but do not remove important story details.\n"
        "- Do not add explanations, markdown, or extra keys.\n\n"
        "Input scenes:\n"
        + json.dumps(source, ensure_ascii=False)
    )

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )

    response = requests.post(
        endpoint,
        params={"key": api_key},
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
        timeout=180,
    )

    if response.status_code != 200:
        detail = response.text[:1200]
        raise RuntimeError(
            f"Gemini caption request failed ({response.status_code}): {detail}"
        )

    try:
        body = response.json()
        generated = body["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(generated)
        captions = parsed["captions"]
    except Exception as exc:
        raise RuntimeError(
            f"Gemini returned invalid caption JSON: {exc}"
        ) from exc

    if not isinstance(captions, list):
        raise RuntimeError("Gemini captions field is not a list.")

    result = {}
    for item in captions:
        if not isinstance(item, dict):
            raise RuntimeError("Gemini returned an invalid caption record.")

        try:
            caption_id = int(item["id"])
            text = str(item["text"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Gemini caption record is missing id or text.") from exc

        if caption_id in result or not text:
            raise RuntimeError("Gemini returned duplicate IDs or empty captions.")

        result[caption_id] = text

    expected_ids = set(range(1, len(records) + 1))
    if set(result) != expected_ids:
        raise RuntimeError(
            "Gemini caption count/IDs do not match the narration scene count."
        )

    return [result[index] for index in range(1, len(records) + 1)]


def main():
    config = read_config(CONFIG_FILE)
    mode = normalize_mode(config.get("CAPTIONS", "hindi"))

    narration = read_json(NARRATION_FILE)
    if narration.get("status") != "completed":
        raise RuntimeError("Narration is not marked completed.")

    records = get_scene_texts(narration)

    if mode == "hindi":
        output_texts = [row["source_text"] for row in records]
        model = None
        print("Hindi captions: preserving original Hindi narration.")
    else:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is missing. Add it in GitHub Actions Secrets."
            )

        model = load_selected_model()
        print(f"Preparing {mode} captions with selected model: {model}")
        output_texts = transform_with_gemini(
            records, mode, model, api_key
        )

    output_records = []
    for row, caption_text in zip(records, output_texts):
        output_records.append({
            "part": row["part"],
            "scene": row["scene"],
            "text": caption_text,
        })

    result = {
        "status": "completed",
        "mode": mode,
        "source": str(NARRATION_FILE),
        "model": model,
        "total_scenes": len(output_records),
        "captions": output_records,
    }

    save_json(OUTPUT_FILE, result)

    # Re-read and validate the saved file rather than assuming the write worked.
    saved = read_json(OUTPUT_FILE)
    if (
        saved.get("status") != "completed"
        or saved.get("mode") != mode
        or len(saved.get("captions", [])) != len(records)
        or any(not str(row.get("text", "")).strip()
               for row in saved.get("captions", []))
    ):
        raise RuntimeError("Saved captions failed validation.")

    print("=" * 55)
    print("CAPTION PREPARATION: SUCCESS")
    print("Mode:", mode)
    print("Scenes:", len(output_records))
    print("Output:", OUTPUT_FILE)
    print("Note: captions are prepared as text; MP4 burn-in is a later step.")
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CAPTION PREPARATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)