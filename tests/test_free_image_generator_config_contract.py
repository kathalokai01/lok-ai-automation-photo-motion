"""Regression test for the image worker's production-settings import contract."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import input_config
import free_image_generator


class FreeImageGeneratorConfigContractTests(unittest.TestCase):
    def test_image_worker_applies_default_production_settings(self):
        config = dict(input_config.DEFAULT_CONFIG)
        config["FORMAT"] = "short"

        free_image_generator.apply_input_settings(config)

        self.assertEqual(
            free_image_generator.IMAGE_PROCESSING_PROFILE,
            "none",
        )

    def test_image_worker_preserves_selected_processing_profile(self):
        config = dict(input_config.DEFAULT_CONFIG)
        config["FORMAT"] = "short"
        config["IMAGE_PROCESSING_PROFILE"] = "cinematic"

        free_image_generator.apply_input_settings(config)

        self.assertEqual(
            free_image_generator.IMAGE_PROCESSING_PROFILE,
            "cinematic",
        )


if __name__ == "__main__":
    unittest.main()
