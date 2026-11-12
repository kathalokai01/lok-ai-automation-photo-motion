import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_depth_maps


class DepthModelSignaturePropagationTests(unittest.TestCase):
    def test_signature_is_attached_to_every_scene_job(self):
        signature = "a" * 64
        jobs = [
            {"scene": 1, "scene_id": "part-1-main-1"},
            {"scene": 2, "scene_id": "part-1-hook"},
            {"scene": 3, "scene_id": "part-1-resolution"},
        ]

        normalized = [
            generate_depth_maps.bind_model_signature(job, signature)
            for job in jobs
        ]

        self.assertEqual(
            [job["model_signature"] for job in normalized],
            [signature, signature, signature],
        )

    def test_invalid_signature_is_rejected(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "Depth model_signature is missing or invalid",
        ):
            generate_depth_maps.bind_model_signature({"scene": 1}, "")

    def test_signature_is_normalized(self):
        normalized = generate_depth_maps.bind_model_signature(
            {"scene": 1},
            "A" * 64,
        )
        self.assertEqual(normalized["model_signature"], "a" * 64)

    def test_model_signature_is_shared_with_stage_metadata(self):
        source = generate_depth_maps.digest_data({
            "model": generate_depth_maps.MODEL_ID,
            "revision": generate_depth_maps.MODEL_REVISION,
            "device": generate_depth_maps.DEVICE_SETTING,
            "ai_required": generate_depth_maps.AI_REQUIRED,
            "fallback_allowed": generate_depth_maps.ALLOW_PSEUDO_FALLBACK,
            "upgrade_fallback": generate_depth_maps.UPGRADE_FALLBACK,
        })
        job = generate_depth_maps.bind_model_signature({"scene": 1}, source)
        self.assertEqual(job["model_signature"], source)


if __name__ == "__main__":
    unittest.main()
