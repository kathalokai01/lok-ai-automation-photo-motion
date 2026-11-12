import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline_preflight


class FailedDepthManifestResumeTests(unittest.TestCase):
    def setUp(self):
        self.visual_jobs = {
            "status": "completed",
            "source_fingerprint": "a" * 64,
            "config_signature": "b" * 64,
            "format": "short",
            "scenes_min": 5,
            "scenes_max": 20,
            "actual_scene_counts": {"1": 10},
        }
        self.depth = {
            "version": "photo-motion-depth-v11-model-signature-propagation",
            "status": "failed",
            "stage": "depth_maps",
            "source_fingerprint": "a" * 64,
            "config_signature": "b" * 64,
            "model_signature": "c" * 64,
            "format": "short",
            "scenes_min": 5,
            "scenes_max": 20,
            "actual_scene_counts": {"1": 10},
            "total_scenes": 12,
            "completed_scenes": 0,
            "scenes": [],
            "last_error": "Depth model failed for scene 1",
        }

    def test_matching_failed_depth_manifest_is_retryable(self):
        self.assertTrue(
            pipeline_preflight.is_retryable_failed_depth_manifest(
                self.depth, self.visual_jobs
            )
        )

    def test_different_source_fingerprint_is_rejected(self):
        self.depth["source_fingerprint"] = "d" * 64
        self.assertFalse(
            pipeline_preflight.is_retryable_failed_depth_manifest(
                self.depth, self.visual_jobs
            )
        )

    def test_different_config_signature_is_rejected(self):
        self.depth["config_signature"] = "d" * 64
        self.assertFalse(
            pipeline_preflight.is_retryable_failed_depth_manifest(
                self.depth, self.visual_jobs
            )
        )

    def test_invalid_model_signature_is_rejected(self):
        self.depth["model_signature"] = "invalid"
        self.assertFalse(
            pipeline_preflight.is_retryable_failed_depth_manifest(
                self.depth, self.visual_jobs
            )
        )

    def test_unfinished_or_inconsistent_scene_count_is_rejected(self):
        self.depth["completed_scenes"] = 1
        self.assertFalse(
            pipeline_preflight.is_retryable_failed_depth_manifest(
                self.depth, self.visual_jobs
            )
        )

    def test_missing_failure_reason_is_rejected(self):
        self.depth["last_error"] = ""
        self.assertFalse(
            pipeline_preflight.is_retryable_failed_depth_manifest(
                self.depth, self.visual_jobs
            )
        )

    def test_noncompleted_visual_source_is_rejected(self):
        self.visual_jobs["status"] = "failed"
        self.assertFalse(
            pipeline_preflight.is_retryable_failed_depth_manifest(
                self.depth, self.visual_jobs
            )
        )


if __name__ == "__main__":
    unittest.main()
