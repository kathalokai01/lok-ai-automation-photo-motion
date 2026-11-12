import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline_preflight


class ManifestRowStatusTests(unittest.TestCase):
    def test_generated_image_row_is_accepted(self):
        self.assertIn(
            "generated",
            pipeline_preflight.allowed_manifest_row_statuses("image_manifest"),
        )

    def test_existing_image_row_is_accepted(self):
        self.assertIn(
            "existing",
            pipeline_preflight.allowed_manifest_row_statuses("image_manifest"),
        )

    def test_image_states_are_not_globally_accepted(self):
        self.assertNotIn(
            "generated",
            pipeline_preflight.allowed_manifest_row_statuses("narration_audio"),
        )
        self.assertNotIn(
            "existing",
            pipeline_preflight.allowed_manifest_row_statuses("story"),
        )

    def test_standard_success_and_nonfinal_states_remain_accepted(self):
        allowed = pipeline_preflight.allowed_manifest_row_statuses("image_manifest")
        for status in ("completed", "success", "reused", "pending", "skipped"):
            self.assertIn(status, allowed)

    def test_unknown_status_remains_rejected(self):
        self.assertNotIn(
            "mystery",
            pipeline_preflight.allowed_manifest_row_statuses("image_manifest"),
        )


if __name__ == "__main__":
    unittest.main()
