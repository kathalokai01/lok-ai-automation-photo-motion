import os
import json
import urllib.request
import urllib.error


API_KEY = os.environ.get("GEMINI_API_KEY")

MODEL_FILE = "output/config/selected_model.json"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


if not API_KEY:
    raise SystemExit("ERROR: GEMINI_API_KEY is not set")


if not os.path.isfile(MODEL_FILE):
    raise SystemExit(
        f"ERROR: Selected model file not found: {MODEL_FILE}"
    )


try:
    with open(MODEL_FILE, "r", encoding="utf-8") as file:
        config = json.load(file)
except Exception as e:
    raise SystemExit(
        f"ERROR: Could not read selected model file: {e}"
    )


if config.get("status") != "selected":
    raise SystemExit(
        "ERROR: Gemini model selection is not in selected state"
    )


MODEL = config.get("model")

if not MODEL:
    raise SystemExit(
        "ERROR: No selected Gemini model found"
    )


print("===== GEMINI API TEST =====")
print(f"Selected model: {MODEL}")


url = (
    f"{BASE_URL}/models/"
    f"{MODEL}:generateContent"
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


request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "x-goog-api-key": API_KEY,
    },
    method="POST",
)


try:
    with urllib.request.urlopen(
        request,
        timeout=60,
    ) as response:

        result = json.loads(
            response.read().decode("utf-8")
        )

    candidates = result.get("candidates", [])

    if not candidates:
        raise SystemExit(
            "Gemini API test failed: "
            "No candidates returned"
        )

    parts = (
        candidates[0]
        .get("content", {})
        .get("parts", [])
    )

    if not parts:
        raise SystemExit(
            "Gemini API test failed: "
            "No response parts returned"
        )

    text = parts[0].get("text", "").strip()

    if not text:
        raise SystemExit(
            "Gemini API test failed: "
            "Empty response"
        )

    print(f"Gemini response: {text}")
    print("Gemini API test: PASSED")
    print("===========================")


except urllib.error.HTTPError as e:

    try:
        body = e.read().decode("utf-8")
    except Exception:
        body = ""

    raise SystemExit(
        f"Gemini API test failed: "
        f"HTTP {e.code}: {e.reason}. "
        f"{body[:500]}"
    )


except Exception as e:

    raise SystemExit(
        f"Gemini API test failed: {e}"
    )
