import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image

from scripts import visual_quality_validator as validator


def response(status, payload=None):
    item = requests.Response()
    item.status_code = status
    item.url = "https://example.invalid/generateContent"
    item.headers = {}
    item._content = json.dumps(payload or {}).encode("utf-8")
    return item


class VisionRetryTests(unittest.TestCase):
    def test_transient_503_retries_then_accepts_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((64, 64), 80).convert("RGB").save(image_path)
            success = response(200, {"candidates": [{"content": {"parts": [{
                "text": json.dumps({
                    "prompt_match": True,
                    "character_consistency": True,
                    "quality_score": 9,
                    "issues": [],
                    "reason": "matches",
                })
            }]}}]})
            with patch.object(validator.requests, "post", side_effect=[
                response(503), success
            ]) as post, patch.object(validator.time, "sleep"):
                result = validator._vision_check(
                    image_path, "A sufficiently detailed complete scene prompt.",
                    "gemini-3.8-flash", "test-key", 20,
                )
            self.assertEqual(post.call_count, 2)
            self.assertEqual(result["quality_score"], 9)

    def test_transient_503_exhaustion_is_reported_as_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((64, 64), 80).convert("RGB").save(image_path)
            with patch.object(validator.requests, "post", side_effect=[
                response(503), response(503), response(503), response(503)
            ]) as post, patch.object(validator.time, "sleep"):
                with self.assertRaises(validator.VisionVerificationUnavailable):
                    validator._vision_check(
                        image_path, "A sufficiently detailed complete scene prompt.",
                        "gemini-3.8-flash", "test-key", 20,
                    )
            self.assertEqual(post.call_count, validator.VISION_HTTP_MAX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
