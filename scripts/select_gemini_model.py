import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone


API_KEY = os.environ.get("GEMINI_API_KEY")

if not API_KEY:
    raise SystemExit("ERROR: GEMINI_API_KEY is not set")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
OUTPUT_FILE = "output/config/selected_model.json"

PREFERRED_MODEL = "gemini-3.5-flash-lite"


def api_request(url, method="GET", payload=None, timeout=60):
    headers = {
        "x-goog-api-key": API_KEY,
        "Content-Type": "application/json",
    }

    data = None

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_available_models():
    url = f"{BASE_URL}/models"
    result = api_request(url)

    models = []

    for model in result.get("models", []):
        name = model.get("name", "")
        methods = model.get("supportedGenerationMethods", [])

        if not name:
            continue

        if "generateContent" not in methods:
            continue

        if not name.startswith("models/"):
            continue

        model_id = name.split("/", 1)[1]

        if "gemini" not in model_id.lower():
            continue

        lower = model_id.lower()

        # Skip models that are clearly not intended for text generation.
        excluded_words = (
            "embedding",
            "aqa",
            "image",
            "vision",
            "audio",
            "tts",
            "robotics",
        )

        if any(word in lower for word in excluded_words):
            continue

        models.append({
            "name": name,
            "model_id": model_id,
            "display_name": model.get("displayName", ""),
            "methods": methods,
        })

    return models


def model_priority(model):
    model_id = model["model_id"].lower()

    # Highest priority: exact requested Lite model.
    if model_id == PREFERRED_MODEL:
        return 0

    # Other Lite models.
    if "lite" in model_id and "flash" in model_id:
        return 10

    # Flash models.
    if "flash" in model_id:
        return 20

    # Other Gemini generation models.
    if "gemini" in model_id:
        return 30

    return 100


def test_model(model):
    model_name = model["model_id"]

    url = (
        f"{BASE_URL}/models/"
        f"{model_name}:generateContent"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": "Reply with exactly: GEMINI_OK"
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 10,
        },
    }

    try:
        result = api_request(
            url,
            method="POST",
            payload=payload,
            timeout=60,
        )

        candidates = result.get("candidates", [])

        if not candidates:
            return False, "No candidates returned"

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])

        if not parts:
            return False, "No response parts returned"

        text = parts[0].get("text", "").strip()

        if not text:
            return False, "Empty model response"

        print(
            f"Model test response [{model_name}]: {text}"
        )

        return True, text

    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""

        return False, (
            f"HTTP {e.code}: "
            f"{e.reason}. "
            f"{body[:300]}"
        )

    except Exception as e:
        return False, str(e)


def save_selection(model, tested_models):
    os.makedirs(
        os.path.dirname(OUTPUT_FILE),
        exist_ok=True,
    )

    result = {
        "status": "selected",
        "model": model["model_id"],
        "model_resource": model["name"],
        "display_name": model.get("display_name", ""),
        "selected_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "tested_models": tested_models,
    }

    temp_file = f"{OUTPUT_FILE}.tmp"

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            result,
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.write("\n")

    os.replace(temp_file, OUTPUT_FILE)

    return result


def main():
    print("===== GEMINI MODEL DISCOVERY =====")

    print("Fetching available Gemini models...")

    try:
        models = get_available_models()
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"ERROR: Failed to list Gemini models: "
            f"HTTP {e.code} {e.reason}"
        )
    except Exception as e:
        raise SystemExit(
            f"ERROR: Failed to list Gemini models: {e}"
        )

    if not models:
        raise SystemExit(
            "ERROR: No compatible Gemini models "
            "with generateContent were found."
        )

    models.sort(key=model_priority)

    print(
        f"Compatible models found: {len(models)}"
    )

    print("===== MODEL PRIORITY =====")

    for index, model in enumerate(models, start=1):
        print(
            f"{index}. "
            f"{model['model_id']}"
        )

    print("==========================")

    tested_models = []

    for model in models:
        model_id = model["model_id"]

        print(
            f"\nTesting model: {model_id}"
        )

        success, detail = test_model(model)

        tested_models.append({
            "model": model_id,
            "success": success,
            "detail": detail[:500],
        })

        if success:
            selected = save_selection(
                model,
                tested_models,
            )

            print(
                "\n===== GEMINI MODEL SELECTED ====="
            )
            print(
                f"Selected model: "
                f"{selected['model']}"
            )
            print(
                f"Saved to: {OUTPUT_FILE}"
            )
            print(
                "================================="
            )

            return

        print(
            f"FAILED: {model_id}"
        )
        print(
            f"Reason: {detail[:500]}"
        )
        print(
            "Trying next compatible model..."
        )

    print(
        "\n===== GEMINI MODEL SELECTION FAILED ====="
    )

    print(
        "All compatible Gemini models failed "
        "the live API test."
    )

    raise SystemExit(1)


if __name__ == "__main__":
    main()
