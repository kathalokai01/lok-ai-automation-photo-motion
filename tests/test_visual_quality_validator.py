import json
import os
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
    def test_successful_vision_check_uses_one_request(self):
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
            with patch.object(
                validator.requests, "post", return_value=success
            ) as post:
                result = validator._vision_check(
                    image_path, "A sufficiently detailed complete scene prompt.",
                    "gemini-3.8-flash", "test-key", 20,
                )
            self.assertEqual(post.call_count, 1)
            self.assertEqual(result["quality_score"], 9)

    def test_hard_free_tier_quota_stops_retries_and_model_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((64, 64), 80).convert("RGB").save(image_path)
            exhausted = response(429, {"error": {"message": (
                "Rate limit exceeded for model (limit: 0 input tokens "
                "per minute on Free Tier)"
            )}})
            with patch.object(
                validator.requests, "post", return_value=exhausted
            ) as post, patch.object(validator.time, "sleep") as sleep:
                with self.assertRaises(validator.VisionQuotaExhausted):
                    validator._vision_check(
                        image_path, "A sufficiently detailed complete scene prompt.",
                        "gemini-3.8-flash", "test-key", 20,
                    )
            self.assertEqual(post.call_count, 1)
            self.assertEqual(sleep.call_count, 0)

    def test_test_mode_suppresses_mocked_vision_usage_events(self):
        with patch.dict(os.environ, {"PHOTO_MOTION_TEST_MODE": "1"}), patch.object(
            validator, "record_provider_attempt"
        ) as record:
            validator._record_vision_provider_attempt(
                "gemini_vision", "gemini-3.8-flash", "accepted", True
            )
        record.assert_not_called()

    def test_ordinary_http_429_without_quota_evidence_remains_retryable(self):
        self.assertFalse(validator.is_hard_vision_quota_error(
            429, "Too many requests; retry after 20 seconds"
        ))
        self.assertTrue(validator.is_hard_vision_quota_error(
            429, "limit: 0 input tokens per minute on Free Tier"
        ))

    def test_hard_quota_does_not_try_other_gemini_vision_models(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((256, 256), 80).convert("RGB").save(image_path)
            with patch.dict(os.environ, {
                "GEMINI_VISION_MODEL_FALLBACKS": "gemini-4-argon,gemini-3.6-flash"
            }), patch.object(
                validator, "_vision_check",
                side_effect=validator.VisionQuotaExhausted("free tier limit: 0"),
            ) as check:
                with self.assertRaises(validator.VisionVerificationUnavailable):
                    validator.validate_scene_image(
                        image_path, "A sufficiently detailed complete scene prompt.",
                        (256, 256), enabled=True, api_key="test-key",
                    )
            self.assertEqual(check.call_count, 1)

    def test_transient_503_retries_then_accepts_success(self):
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
            with patch.object(
                validator.requests, "post",
                side_effect=[response(503), response(503), success],
            ) as post, patch.object(validator.time, "sleep") as sleep:
                result = validator._vision_check(
                    image_path, "A sufficiently detailed complete scene prompt.",
                    "gemini-3.8-flash", "test-key", 20,
                )
            self.assertEqual(post.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(result["quality_score"], 9)

    def test_read_timeout_retries_with_bounded_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((64, 64), 80).convert("RGB").save(image_path)
            with patch.object(
                validator.requests, "post",
                side_effect=requests.ReadTimeout("temporary read timeout"),
            ) as post, patch.object(validator.time, "sleep") as sleep:
                with self.assertRaises(validator.VisionVerificationUnavailable):
                    validator._vision_check(
                        image_path, "A sufficiently detailed complete scene prompt.",
                        "gemini-3.8-flash", "test-key", 20,
                    )
            self.assertEqual(post.call_count, validator.VISION_HTTP_MAX_ATTEMPTS)
            self.assertEqual(sleep.call_count, validator.VISION_HTTP_MAX_ATTEMPTS - 1)

    def test_vision_rejection_fails_mandatory_semantic_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((256, 256), 80).convert("RGB").save(image_path)
            rejected = {
                "prompt_match": False,
                "character_consistency": False,
                "quality_score": 3,
                "issues": ["scene mismatch"],
                "reason": "candidate does not depict the requested scene",
            }
            with patch.object(validator, "_vision_check", return_value=rejected):
                result = validator.validate_scene_image(
                    image_path, "A sufficiently detailed complete scene prompt.",
                    (256, 256), enabled=True, minimum_score=5,
                    api_key="test-key",
                )
            self.assertFalse(result["accepted"])
            self.assertFalse(result["vision_accepted"])
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["semantic_story_match"], "not_verified")

    def test_ocr_text_detection_rejects_image_before_vision_call(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((256, 256), 80).convert("RGB").save(image_path)
            with patch.object(
                validator, "detect_embedded_text",
                return_value=[{"text": "Orion", "confidence": 91.0}],
            ), patch.object(validator, "_vision_check") as vision:
                result = validator.validate_scene_image(
                    image_path, "A sufficiently detailed complete scene prompt.",
                    (256, 256), enabled=True, api_key="test-key",
                )
            self.assertFalse(result["accepted"])
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["text_detection"]["status"], "readable_text_detected")
            vision.assert_not_called()


    def test_embedded_text_issue_hard_rejects_candidate_even_if_model_says_match(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((256, 256), 80).convert("RGB").save(image_path)
            result = {
                "prompt_match": True,
                "character_consistency": True,
                "quality_score": 9,
                "issues": ["unwanted_text_in_image"],
                "reason": "readable text is visible in the image",
            }
            with patch.object(validator, "_vision_check", return_value=result):
                checked = validator.validate_scene_image(
                    image_path, "A sufficiently detailed complete scene prompt.",
                    (256, 256), enabled=True, minimum_score=7,
                    api_key="test-key",
                )
            self.assertFalse(checked["accepted"])
            self.assertFalse(checked["vision_accepted"])
            self.assertEqual(checked["status"], "rejected")


    def test_vision_timeout_remains_pending_for_later_reverification(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((256, 256), 80).convert("RGB").save(image_path)
            with patch.object(
                validator, "_vision_check",
                side_effect=validator.VisionVerificationUnavailable("timeout"),
            ):
                with self.assertRaises(validator.VisionVerificationUnavailable):
                    validator.validate_scene_image(
                        image_path, "A sufficiently detailed complete scene prompt.",
                        (256, 256), enabled=True, minimum_score=5,
                        api_key="test-key",
                    )

    def test_unexpected_vision_exception_is_wrapped_as_verification_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((256, 256), 80).convert("RGB").save(image_path)
            with patch.object(
                validator, "_vision_check",
                side_effect=TypeError("'NoneType' object is not subscriptable"),
            ):
                with self.assertRaises(validator.VisionVerificationUnavailable):
                    validator.validate_scene_image(
                        image_path, "A sufficiently detailed complete scene prompt.",
                        (256, 256), enabled=True, minimum_score=5,
                        api_key="test-key",
                    )

    def test_configured_vision_model_fallback_is_used_after_primary_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((256, 256), 80).convert("RGB").save(image_path)
            success = {
                "prompt_match": True,
                "character_consistency": True,
                "quality_score": 9,
                "issues": [],
                "reason": "matches",
            }
            unavailable = validator.VisionVerificationUnavailable("HTTP 429")
            with patch.dict(
                os.environ,
                {"GEMINI_MODEL_FALLBACKS": "gemini-2.5-flash,gemini-2.5-pro"},
            ), patch.object(
                validator,
                "_vision_check",
                side_effect=[unavailable, success],
            ) as vision_check:
                result = validator.validate_scene_image(
                    image_path,
                    "A sufficiently detailed complete scene prompt.",
                    (256, 256),
                    enabled=True,
                    minimum_score=7,
                    model="gemini-3.8-flash",
                    api_key="test-key",
                )
            self.assertTrue(result["accepted"])
            self.assertEqual(result["vision"]["quality_score"], 9)
            self.assertEqual(vision_check.call_count, 2)
            self.assertEqual(
                vision_check.call_args_list[0].args[2],
                "gemini-3.8-flash",
            )
            self.assertEqual(
                vision_check.call_args_list[1].args[2],
                "gemini-2.5-flash",
            )
            self.assertEqual(
                vision_check.call_args_list[1].kwargs["max_attempts"],
                2,
            )

    def test_vision_fallback_models_are_deduplicated_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            Image.effect_noise((256, 256), 80).convert("RGB").save(image_path)
            unavailable = validator.VisionVerificationUnavailable("HTTP 429")
            with patch.dict(
                os.environ,
                {
                    "GEMINI_MODEL_FALLBACKS":
                        "gemini-3.8-flash,gemini-2.5-flash,gemini-2.5-pro,"
                        "gemini-2.5-flash,invalid/model,gemini-2.0-flash"
                },
            ), patch.object(
                validator,
                "_vision_check",
                side_effect=unavailable,
            ) as vision_check:
                with self.assertRaises(
                    validator.VisionVerificationUnavailable
                ):
                    validator.validate_scene_image(
                        image_path,
                        "A sufficiently detailed complete scene prompt.",
                        (256, 256),
                        enabled=True,
                        api_key="test-key",
                    )
            self.assertEqual(vision_check.call_count, 4)
            self.assertEqual(
                [call.args[2] for call in vision_check.call_args_list],
                [
                    "gemini-3.8-flash",
                    "gemini-2.5-flash",
                    "gemini-2.5-pro",
                    "gemini-2.0-flash",
                ],
            )

    def test_offline_ocr_detects_readable_english_and_hindi_words(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "candidate.png"
            image_path.write_bytes(b"fake-image-for-mocked-ocr")
            languages = type("Result", (), {"stdout": "List of available languages (2):\neng\nhin\n"})()
            tsv = type("Result", (), {
                "stdout": "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
                          "5\t1\t1\t1\t1\t1\t10\t10\t80\t20\t91.2\tOrion\n"
            })()
            with patch.object(validator.shutil, "which", return_value="tesseract"), patch.object(
                validator.subprocess, "run", side_effect=[languages, tsv]
            ):
                found = validator.detect_embedded_text(image_path)
            self.assertEqual(found[0]["text"], "Orion")
            self.assertEqual(found[0]["confidence"], 91.2)


    def test_vision_policy_rejects_any_embedded_readable_text(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "scripts" / "visual_quality_validator.py"
        ).read_text(encoding="utf-8")
        self.assertIn("ZERO-TEXT POLICY (mandatory)", source)
        self.assertIn("unwanted_text_in_image", source)
        self.assertIn("rule has no story-based exception", source)

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
    def test_provider_prompt_bans_all_story_text_and_written_objects(self):
        prompt = image_generator.make_prompt({
            "source_job": {},
            "prompt": "A cinematic engineer in a server room beside a computer.",
            "narration": "The engineer sees a warning on the monitor.",
            "role": "main",
            "negative_prompt": "",
        })
        self.assertIn("ABSOLUTE IMAGE RULE", prompt)
        self.assertIn("absolute ban", prompt.casefold())
        self.assertIn("blank, unmarked surfaces", prompt.casefold())
        negative = image_generator.build_negative_prompt({
            "source_job": {},
            "prompt": "A cinematic engineer in a server room.",
            "narration": "The engineer sees a warning.",
            "role": "main",
            "negative_prompt": "",
        })
        self.assertIn("text", negative.casefold())


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
