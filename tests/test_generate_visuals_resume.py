from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import checkpoint_manager, generate_visuals
from tests.test_checkpoint_manager_visual_resume import make_visual_checkpoint


class GenerateVisualsResumeTests(unittest.TestCase):
    def test_twelve_restored_manifest_images_are_reused_without_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, hashes, source_fp, config_sig = make_visual_checkpoint(root, count=12)
            output = root / "output"
            self.assertEqual(
                checkpoint_manager.restore_partial_visual_images(source, output), 12
            )
            progress_path = output / "checkpoints" / "visual_jobs_progress.json"
            rows = generate_visuals.load_previous_progress if False else None
            jobs = []
            saved = __import__("json").loads(progress_path.read_text(encoding="utf-8"))
            for index, row in enumerate(saved["scene_progress"], 1):
                jobs.append({
                    "scene_id": row["scene_id"], "part": 1,
                    "global_scene": index, "local_scene": index,
                    "scene": index, "source_scene_number": index,
                    "role": "main", "image_path": row["image_path"],
                    "prompt_signature": row["prompt_signature"],
                    "source_scene_signature": row["source_scene_signature"],
                    "source_fingerprint": source_fp,
                    "config_signature": config_sig,
                    "image_width": 720, "image_height": 1280,
                })

            def resolve_saved(relative):
                return output / relative.removeprefix("output/")

            def verify_saved(path, width, height):
                return {"sha256": hashes[Path(path).name], "integrity": "verified"}

            with patch.object(generate_visuals, "CHECKPOINT_FILE", progress_path), \
                 patch.object(generate_visuals, "resolve_image_path", side_effect=resolve_saved), \
                 patch.object(generate_visuals, "validate_png", side_effect=verify_saved):
                previous = generate_visuals.load_previous_progress()
                rebuilt = generate_visuals.build_progress(
                    jobs, previous, source_fp, config_sig
                )

            self.assertEqual(len(previous), 12)
            self.assertEqual(rebuilt["completed_jobs"], 12)
            self.assertEqual(rebuilt["remaining_jobs"], 0)
            self.assertTrue(all(row["status"] == "existing" for row in rebuilt["scene_progress"]))


if __name__ == "__main__":
    unittest.main()
