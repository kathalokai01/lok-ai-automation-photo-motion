
#!/usr/bin/env python3
"""Prepare validated Hindi, English, or Hinglish scene captions."""

from __future__ import annotations

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


def log(*items):
    print(*items, flush=True)


def read_json(path):
    if not path.is_file():
        raise RuntimeError(f"Required file not found: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
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
            f"Unsupported CAPTIONS mode: {mode!r}. "
            "Use hindi, english, or hinglish."
        )

    return aliases[mode]


def get_scene_texts(narration):
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
            scene = int(item["scene"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Narration record {index} needs numeric part and scene fields."
            ) from exc

        if part < 1 or scene < 1:
            raise RuntimeError(
                f"Part and scene numbers must be positive: {item}"
            )

        text = str(item.get("text", "")).strip()

        if not text:
            raise RuntimeError(
                f"Empty narration at Part {part}, Scene {scene}."
            )

        key = (part, scene)

        if key in seen:
            raise RuntimeError(
                f"Duplicate narration scene: Part {part}, Scene {scene}."
            )

        seen.add(key)

        records.append({
            "part": part,
            "scene": scene,
            "source_text": text,
        })

    return sorted(records, key=lambda row: (row["part"], row["scene"]))


def load_selected_model():
    data = read_json(MODEL_FILE)

    if not isinstance(data, dict) or data.get("status") != "selected":
        raise RuntimeError(
            "Gemini model selection is not marked as selected."
        )

    model = str(data.get("model", "")).strip()

    if not model:
        raise RuntimeError("Selected model file has no model name.")

    # Prevent malformed model names from changing the API endpoint.
    if "/" in model or ":" in model or any(c.isspace() for c in model):
        raise RuntimeError(f"Invalid selected Gemini model name: {model!r}")

    return model


def transform_with_gemini(records, mode, model, api_key):
    if mode == "english":
        instruction = (
            "Translate each Hindi narration line into natural, faithful English. "
            "Do not summarize, add events, or omit meaning."
        )
    else:
        instruction = (
            "Transliterate each Hindi narration line into natural, readable "
            "Hinglish using Latin letters. Preserve the Hindi words and meaning; "
            "do not translate them into English, summarize, or add content."
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
        "You prepare captions for a story video.\n"
        f"Task: {instruction}\n"
        "Return ONLY valid JSON in this exact shape:\n"
        '{"captions":[{"id":1,"text":"..."}]}\n'
        "Rules:\n"
        "- Include every input ID exactly once.\n"
        "- Preserve scene order and original story meaning.\n"
        "- Keep each caption readable and concise without omitting key meaning.\n"
        "- Do not include markdown or explanations.\n\n"
        "Input scenes:\n"
        + json.dumps(source, ensure_ascii=False)
    )

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
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
        # Avoid printing request headers or the API key.
        detail = response.text[:1200]
        raise RuntimeError(
            f"Gemini caption request failed ({response.status_code}): {detail}"
        )

    try:
        body = response.json()
        generated = body["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(generated)
        captions = parsed["captions"]
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise RuntimeError(
            f"Gemini returned invalid caption JSON: {exc}"
        ) from exc

    if not isinstance(captions, list):
        raise RuntimeError("Gemini captions field is not a list.")

    by_id = {}

    for item in captions:
        if not isinstance(item, dict):
            raise RuntimeError("Gemini returned an invalid caption record.")

        try:
            caption_id = int(item["id"])
            text = str(item["text"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Gemini caption record is missing a valid ID or text."
            ) from exc

        if caption_id in by_id or not text:
            raise RuntimeError(
                "Gemini returned duplicate IDs or empty captions."
            )

        by_id[caption_id] = text

    expected_ids = set(range(1, len(records) + 1))

    if set(by_id) != expected_ids:
        raise RuntimeError(
            "Gemini caption IDs do not match the narration scene count."
        )

    return [by_id[index] for index in range(1, len(records) + 1)]


def main():
    config = read_config(CONFIG_FILE)
    mode = normalize_mode(config.get("CAPTIONS", "hindi"))

    narration = read_json(NARRATION_FILE)
    records = get_scene_texts(narration)

    model = None

    if mode == "hindi":
        output_texts = [row["source_text"] for row in records]
        log("Hindi captions: preserving original narration.")
    else:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()

        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is missing. Add it in GitHub Actions Secrets."
            )

        model = load_selected_model()
        log(f"Preparing {mode} captions with selected model: {model}")

        output_texts = transform_with_gemini(
            records,
            mode,
            model,
            api_key,
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
        "format": config.get("FORMAT", "short").strip().lower(),
        "source": str(NARRATION_FILE.relative_to(ROOT)),
        "model": model,
        "total_scenes": len(output_records),
        "captions": output_records,
    }

    save_json(OUTPUT_FILE, result)

    saved = read_json(OUTPUT_FILE)

    if (
        not isinstance(saved, dict)
        or saved.get("status") != "completed"
        or saved.get("mode") != mode
        or len(saved.get("captions", [])) != len(records)
        or any(
            not str(row.get("text", "")).strip()
            for row in saved.get("captions", [])
        )
    ):
        raise RuntimeError("Saved captions failed validation.")

    log("=" * 55)
    log("CAPTION PREPARATION: SUCCESS")
    log("Mode:", mode)
    log("Scenes:", len(output_records))
    log("Output:", OUTPUT_FILE.relative_to(ROOT))
    log("Note: MP4 subtitle burn-in is a later workflow step.")
    log("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CAPTION PREPARATION FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
