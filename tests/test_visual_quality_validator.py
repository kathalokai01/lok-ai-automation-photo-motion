import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import visual_quality_validator as validator
import free_image_generator as image_generator


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

    def test_read_timeout_retries_then_accepts_valid_json(self):
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
                requests.ReadTimeout("temporary read timeout"), success
            ]) as post, patch.object(validator.time, "sleep"):
                result = validator._vision_check(
                    image_path, "A sufficiently detailed complete scene prompt.",
                    "gemini-3.8-flash", "test-key", 20,
                )
            self.assertEqual(post.call_count, 2)
            self.assertEqual(result["quality_score"], 9)

    def test_connection_timeout_exhaustion_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((64, 64), 80).convert("RGB").save(image_path)
            with patch.object(
                validator.requests, "post",
                side_effect=requests.ReadTimeout("temporary read timeout"),
            ) as post, patch.object(validator.time, "sleep"):
                with self.assertRaises(validator.VisionVerificationUnavailable):
                    validator._vision_check(
                        image_path, "A sufficiently detailed complete scene prompt.",
                        "gemini-3.8-flash", "test-key", 20,
                    )
            self.assertEqual(post.call_count, validator.VISION_HTTP_MAX_ATTEMPTS)

    def test_vision_rejection_is_advisory_for_technically_valid_image(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((256, 256), 80).convert("RGB").save(image_path)
            rejected = {
                "prompt_match": False,
                "character_consistency": False,
                "quality_score": 3,
                "issues": ["possible prompt mismatch"],
                "reason": "low confidence",
            }
            with patch.object(validator, "_vision_check", return_value=rejected):
                result = validator.validate_scene_image(
                    image_path, "A sufficiently detailed complete scene prompt.",
                    (256, 256), enabled=True, minimum_score=5,
                    api_key="test-key",
                )
            self.assertTrue(result["accepted"])
            self.assertFalse(result["vision_accepted"])
            self.assertEqual(result["status"], "accepted_with_warning")
            self.assertEqual(result["semantic_story_match"], "not_verified")

    def test_vision_timeout_is_advisory_for_technically_valid_image(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((256, 256), 80).convert("RGB").save(image_path)
            with patch.object(
                validator, "_vision_check",
                side_effect=validator.VisionVerificationUnavailable("timeout"),
            ):
                result = validator.validate_scene_image(
                    image_path, "A sufficiently detailed complete scene prompt.",
                    (256, 256), enabled=True, minimum_score=5,
                    api_key="test-key",
                )
            self.assertTrue(result["accepted"])
            self.assertIsNone(result["vision"])
            self.assertEqual(result["status"], "accepted_with_warning")
            self.assertIn("timeout", result["verification_warning"])

    def test_missing_or_corrupt_image_still_fails_technical_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            image_path.write_bytes(b"not an image")
            with self.assertRaises(RuntimeError):
                validator.validate_scene_image(
                    image_path, "A sufficiently detailed complete scene prompt.",
                    (256, 256), enabled=False,
                )


class ImageGeneratorRegressionTests(unittest.TestCase):
    def test_parse_size_accepts_requested_dimension_tuple(self):
        self.assertEqual(image_generator.parse_size((720, 1280)), (720, 1280))
        self.assertEqual(image_generator.parse_size([1280, 720]), (1280, 720))
        self.assertEqual(image_generator.parse_size("720x1280"), (720, 1280))

    def test_vision_score_is_optional_when_advisory_vision_is_unavailable(self):
        self.assertIsNone(image_generator.vision_quality_score({
            "accepted": True,
            "status": "accepted_with_warning",
            "vision": None,
        }))
        self.assertIsNone(image_generator.vision_quality_score({
            "accepted": True,
            "vision": "unavailable",
        }))

    def test_vision_score_reads_numeric_evidence(self):
        self.assertEqual(
            image_generator.vision_quality_score({
                "vision": {"quality_score": 8}
            }),
            8,
        )
        self.assertIsNone(image_generator.vision_quality_score({
            "vision": {"quality_score": True}
        }))


if __name__ == "__main__":
    unittest.main()
