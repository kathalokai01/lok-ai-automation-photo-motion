import json
import sys
import tempfile
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

    def test_completed_manifest_with_missing_scene_rows_is_retryable(self):
        self.manifest["summary"]["total_jobs"] = 3
        self.assertTrue(
            pipeline_preflight.is_retryable_completed_tts_integrity_issue(
                self.manifest
            )
        )

    def test_duplicate_scene_ids_are_not_retryable(self):
        self.manifest["jobs"][1]["scene_id"] = self.manifest["jobs"][0]["scene_id"]
        self.assertFalse(
            pipeline_preflight.is_retryable_completed_tts_integrity_issue(
                self.manifest
            )
        )


class InProgressImageManifestRetryTests(unittest.TestCase):
    def test_identity_matched_partial_image_manifest_is_retryable(self):
        with tempfile.TemporaryDirectory() as temporary:
            original_out = pipeline_preflight.OUT
            try:
                pipeline_preflight.OUT = Path(temporary)
                visual_dir = Path(temporary) / "visuals"
                visual_dir.mkdir(parents=True)
                source = "a" * 64
                config = "b" * 64
                jobs = [
                    {"scene_id": "part_01_hook"},
                    {"scene_id": "part_01_scene_01"},
                ]
                (visual_dir / "visual_jobs_manifest.json").write_text(
                    json.dumps({
                        "source_fingerprint": source,
                        "config_signature": config,
                        "format": "short",
                        "total_jobs": 2,
                        "scene_order": jobs,
                    }),
                    encoding="utf-8",
                )
                manifest = {
                    "status": "in_progress",
                    "source_fingerprint": source,
                    "config_signature": config,
                    "format": "short",
                    "total_scenes": 2,
                    "images_required": 2,
                    "images_ready": 1,
                    "images_failed": 0,
                    "images_pending": 1,
                    "scenes": [
                        {"scene_id": row["scene_id"], "source_fingerprint": source,
                         "config_signature": config, "status": "generated"}
                        for row in jobs
                    ],
                }
                self.assertTrue(
                    pipeline_preflight.is_retryable_in_progress_image_manifest(
                        manifest
                    )
                )
            finally:
                pipeline_preflight.OUT = original_out

    def test_image_manifest_with_wrong_source_identity_is_not_retryable(self):
        with tempfile.TemporaryDirectory() as temporary:
            original_out = pipeline_preflight.OUT
            try:
                pipeline_preflight.OUT = Path(temporary)
                visual_dir = Path(temporary) / "visuals"
                visual_dir.mkdir(parents=True)
                (visual_dir / "visual_jobs_manifest.json").write_text(
                    json.dumps({
                        "source_fingerprint": "a" * 64,
                        "config_signature": "b" * 64,
                        "format": "short",
                        "total_jobs": 1,
                        "scene_order": [{"scene_id": "part_01_scene_01"}],
                    }),
                    encoding="utf-8",
                )
                manifest = {
                    "status": "in_progress",
                    "source_fingerprint": "c" * 64,
                    "config_signature": "b" * 64,
                    "format": "short",
                    "total_scenes": 1,
                    "images_required": 1,
                    "images_ready": 0,
                    "images_failed": 0,
                    "images_pending": 1,
                    "scenes": [{
                        "scene_id": "part_01_scene_01",
                        "source_fingerprint": "c" * 64,
                        "config_signature": "b" * 64,
                        "status": "pending",
                    }],
                }
                self.assertFalse(
                    pipeline_preflight.is_retryable_in_progress_image_manifest(
                        manifest
                    )
                )
            finally:
                pipeline_preflight.OUT = original_out


if __name__ == "__main__":
    unittest.main()
