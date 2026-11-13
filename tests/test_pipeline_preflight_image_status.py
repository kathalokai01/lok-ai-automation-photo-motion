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


class CompletedTTSIntegrityRetryTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            "status": "completed",
            "source_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "jobs": [
                {"scene_id": "part_01_hook", "status": "completed"},
                {"scene_id": "part_01_scene_01", "status": "completed"},
            ],
            "parts": [{"part": 1, "audio_path": "output/narration/part-01.mp3"}],
            "summary": {"total_jobs": 2, "completed_jobs": 2},
        }

    def test_completed_identity_matched_manifest_can_retry_corrupt_audio(self):
        self.assertTrue(
            pipeline_preflight.is_retryable_completed_tts_integrity_issue(
                self.manifest
            )
        )

    def test_invalid_source_identity_is_not_retryable(self):
        self.manifest["source_sha256"] = "not-a-sha256"
        self.assertFalse(
            pipeline_preflight.is_retryable_completed_tts_integrity_issue(
                self.manifest
            )
        )

    def test_incomplete_completed_manifest_is_not_retryable(self):
        self.manifest["summary"]["completed_jobs"] = 1
        self.assertFalse(
            pipeline_preflight.is_retryable_completed_tts_integrity_issue(
                self.manifest
            )
        )

    def test_noncompleted_manifest_is_not_handled_by_completed_retry_policy(self):
        self.manifest["status"] = "failed"
        self.assertFalse(
            pipeline_preflight.is_retryable_completed_tts_integrity_issue(
                self.manifest
            )
        )
