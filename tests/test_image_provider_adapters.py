#!/usr/bin/env python3
"""Unit tests for the optional OVHcloud and AI Horde image adapters."""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scripts import image_provider_adapters as adapters
import free_image_generator as image_generator


class FakeResponse:
    def __init__(self, payload=None, content=b"", status_code=200, headers=None):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("invalid json")
        return self._payload


class ImageProviderAdapterTests(unittest.TestCase):

    def test_cloudflare_daily_free_allocation_is_quota_not_transient_rate_limit(self):
        error = RuntimeError(
            "HTTP 429: AiError: you have used up your daily free allocation "
            "of 10,000 neurons"
        )
        error.response = SimpleNamespace(
            status_code=429,
            text=(
                "AiError: you have used up your daily free allocation "
                "of 10,000 neurons"
            ),
            content=b"",
        )
        result = image_generator.classify_provider_error(error)
        self.assertEqual(result["category"], "quota_or_billing")
        self.assertTrue(result["stop_retry"])
        self.assertTrue(result["stop_billable"])

    def test_ordinary_http_429_remains_a_transient_rate_limit(self):
        error = RuntimeError("HTTP 429: too many requests per minute")
        error.response = SimpleNamespace(
            status_code=429,
            text="too many requests per minute",
            content=b"",
        )
        result = image_generator.classify_provider_error(error)
        self.assertEqual(result["category"], "rate_limit")
        self.assertFalse(result["stop_billable"])

    def test_cloudflare_models_share_one_account_quota_block(self):
        blocked = ["cloudflare"]
        self.assertEqual(
            image_generator.billable_provider_group(
                "cloudflare:@cf/black-forest-labs/flux-1-schnell"
            ),
            "cloudflare",
        )
        self.assertTrue(
            image_generator.billable_provider_is_blocked(
                "cloudflare:@cf/black-forest-labs/flux-2-klein-4b",
                True,
                blocked,
            )
        )
        self.assertFalse(
            image_generator.billable_provider_is_blocked(
                "huggingface:mrfakename/Z-Image-Turbo",
                False,
                blocked,
            )
        )
    def test_openai_b64_json_payload_is_decoded(self):
        raw = b"image-bytes"
        result = adapters._image_from_payload(
            {"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]},
            "test",
        )
        self.assertEqual(result, raw)

    def test_missing_image_payload_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "no usable image"):
            adapters._image_from_payload({"data": []}, "test")

    def test_invalid_base64_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "invalid base64"):
            adapters._image_from_payload(
                {"data": [{"b64_json": "%%%"}]}, "test"
            )

    def test_ovh_supports_anonymous_raw_image_response(self):
        fake = FakeResponse(
            content=b"raw-image-bytes",
            headers={"content-type": "application/octet-stream"},
        )
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(adapters.SESSION, "post", return_value=fake) as post:
                result = adapters.generate_ovhcloud_image(
                    "a detailed scene prompt", (720, 1280), 1, "blurry"
                )
        self.assertEqual(result, b"raw-image-bytes")
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])
        self.assertIn("negative_prompt", post.call_args.kwargs["json"])

    def test_horde_default_model_is_stable_sdxl(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(adapters.os.getenv("AIHORDE_IMAGE_MODEL", "stable_diffusion_xl"), "stable_diffusion_xl")


if __name__ == "__main__":
    unittest.main()
