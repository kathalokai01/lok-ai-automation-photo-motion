#!/usr/bin/env python3

import json
import os
import time
import urllib.request
from pathlib import Path


INPUT_TOPIC = Path("Input/topic.txt")
INPUT_STORY = Path("output/story/ai_story.json")
MODEL_FILE = Path("output/config/selected_model.json")

OUTPUT_DIR = Path("output/publish")
OUTPUT_JSON = OUTPUT_DIR / "youtube_metadata.json"
OUTPUT_TITLE = OUTPUT_DIR / "youtube_title.txt"
OUTPUT_DESCRIPTION = OUTPUT_DIR / "youtube_description.txt"
OUTPUT_TAGS = OUTPUT_DIR / "youtube_tags.txt"

MAX_RETRIES = 3


def load_json(path):
    if not path.exists():
        raise SystemExit(f"ERROR: File not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_topic():
    if not INPUT_TOPIC.exists():
        raise SystemExit(f"ERROR: File not found: {INPUT_TOPIC}")

    text = INPUT_TOPIC.read_text(encoding="utf-8")

    for line in text.splitlines():
        line = line.strip()

        if line.startswith("TOPIC"):
            if "=" in line:
                value = line.split("=", 1)[1].strip()
                return value.strip("\"'")

    raise SystemExit("ERROR: TOPIC not found in Input/topic.txt")


def load_model():
    config = load_json(MODEL_FILE)

    if config.get("status") != "selected":
        raise SystemExit("ERROR: Gemini model is not selected")

    model = config.get("model")

    if not model:
        raise SystemExit("ERROR: Gemini model not found")

    return model


def save_outputs(data):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    OUTPUT_JSON.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    OUTPUT_TITLE.write_text(
        data["title"] + "\n",
        encoding="utf-8"
    )

    OUTPUT_DESCRIPTION.write_text(
        data["description"] + "\n",
        encoding="utf-8"
    )

    OUTPUT_TAGS.write_text(
        ", ".join(data["tags"]) + "\n",
        encoding="utf-8"
    )


def generate_metadata(api_key, model, topic, story):

    schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string"
            },
            "description": {
                "type": "string"
            },
            "tags": {
                "type": "array",
                "items": {
                    "type": "string"
                }
            }
        },
        "required": [
            "title",
            "description",
            "tags"
        ]
    }

    prompt = f"""
You are a YouTube metadata specialist for a Hindi cinematic
storytelling channel.

Create YouTube upload metadata for the following video.

TOPIC:
{topic}

STORY:
{json.dumps(story, ensure_ascii=False)[:30000]}

Requirements:

- Title must be natural Hindi/Hinglish.
- Make the title interesting without fake claims.
- Do not use excessive clickbait.
- Do not use ALL CAPS.
- Keep the title suitable for a YouTube video.
- Description should be in natural Hindi.
- Description should briefly explain the story/video.
- Include relevant searchable keywords naturally.
- Do not claim facts that are not present in the story.
- Do not include URLs.
- Do not include instructions to subscribe or like unless appropriate.
- Generate 10 to 20 relevant YouTube tags.
- Tags should be relevant to the story, Hindi storytelling,
  mystery/cinematic content when applicable.
- Return ONLY the requested JSON structure.
"""

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema
        }
    }

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )

    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key
        },
        method="POST"
    )

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            print(
                f"Generating YouTube metadata "
                f"(attempt {attempt}/{MAX_RETRIES})..."
            )

            with urllib.request.urlopen(
                request,
                timeout=120
            ) as response:

                result = json.loads(
                    response.read().decode("utf-8")
                )

            text = (
                result["candidates"][0]
                ["content"]["parts"][0]["text"]
            )

            metadata = json.loads(text)

            title = str(
                metadata.get("title", "")
            ).strip()

            description = str(
                metadata.get("description", "")
            ).strip()

            tags = metadata.get("tags", [])

            if not title:
                raise ValueError("Generated title is empty")

            if not description:
                raise ValueError(
                    "Generated description is empty"
                )

            if not isinstance(tags, list) or not tags:
                raise ValueError(
                    "Generated tags are empty"
                )

            tags = [
                str(tag).strip()
                for tag in tags
                if str(tag).strip()
            ]

            return {
                "status": "completed",
                "topic": topic,
                "model": model,
                "title": title,
                "description": description,
                "tags": tags
            }

        except Exception as e:

            last_error = e

            print(
                f"Metadata generation failed: {e}"
            )

            if attempt < MAX_RETRIES:
                time.sleep(3)

    raise SystemExit(
        f"ERROR: Metadata generation failed after "
        f"{MAX_RETRIES} attempts: {last_error}"
    )


api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    raise SystemExit(
        "ERROR: GEMINI_API_KEY is not set"
    )


topic = read_topic()
model = load_model()
story = load_json(INPUT_STORY)

metadata = generate_metadata(
    api_key,
    model,
    topic,
    story
)

save_outputs(metadata)

print("======================================")
print("YOUTUBE METADATA GENERATED")
print("======================================")
print(f"Title: {metadata['title']}")
print(f"Tags: {len(metadata['tags'])}")
print(f"JSON: {OUTPUT_JSON}")
print(f"Title file: {OUTPUT_TITLE}")
print(f"Description file: {OUTPUT_DESCRIPTION}")
print(f"Tags file: {OUTPUT_TAGS}")
print("======================================")
