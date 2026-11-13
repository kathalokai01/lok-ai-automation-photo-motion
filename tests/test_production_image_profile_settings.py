"""Regression tests for the production image profile configuration API."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import input_config


class ProductionImageProfileTests(unittest.TestCase):
    def test_missing_profile_defaults_to_no_processing(self):
        self.assertEqual(
            input_config.get_production_settings({}),
            {"image_profile": "none"},
        )

    def test_supported_profiles_are_preserved(self):
        for profile in ("none", "natural", "cinematic", "soft"):
            with self.subTest(profile=profile):
                self.assertEqual(
                    input_config.get_production_settings(
                        {"IMAGE_PROCESSING_PROFILE": profile}
                    )["image_profile"],
                    profile,
                )

    def test_profile_is_normalized(self):
        self.assertEqual(
            input_config.get_production_settings(
                {"IMAGE_PROCESSING_PROFILE": " CINEMATIC "}
            )["image_profile"],
            "cinematic",
        )

    def test_unsupported_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "IMAGE_PROCESSING_PROFILE"):
            input_config.get_production_settings(
                {"IMAGE_PROCESSING_PROFILE": "ultra"}
            )


if __name__ == "__main__":
    unittest.main()
