import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render_video
import validate_final_output


class MainSceneNumberContractTests(unittest.TestCase):
    def test_source_scene_number_wins_over_visual_slot(self):
        row = {
            "role": "main",
            "local_scene": 2,
            "source_scene_number": 1,
            "scene": 2,
        }
        self.assertEqual(
            render_video.main_scene_number(row, "test scene"),
            1,
        )

    def test_legacy_row_falls_back_to_local_scene(self):
        row = {"role": "main", "local_scene": 3}
        self.assertEqual(
            render_video.main_scene_number(row, "test scene"),
            3,
        )

    def test_invalid_explicit_source_number_is_not_hidden_by_slot(self):
        row = {
            "role": "main",
            "local_scene": 2,
            "source_scene_number": 0,
        }
        with self.assertRaises(RuntimeError):
            render_video.main_scene_number(row, "test scene")

    def test_hook_slot_does_not_shift_main_scene_sequence(self):
        rows = [
            {
                "part": 1,
                "role": "main",
                "local_scene": scene + 1,
                "source_scene_number": scene,
            }
            for scene in range(1, 11)
        ]
        manifest = {"actual_scene_counts": {"1": 10}}
        counts = render_video.actual_counts_from_photo_rows(
            manifest, rows, [1], 5, 20
        )
        self.assertEqual(counts, {1: 10})





class FinalValidatorSceneNumberContractTests(unittest.TestCase):
    def test_source_scene_number_wins_over_global_visual_slot(self):
        row = {
            "part": 1,
            "role": "main",
            "scene_id": "part_01_scene_01",
            "local_scene": 2,
            "source_scene_number": 1,
            "scene": 2,
        }
        self.assertEqual(
            validate_final_output.scene_key(row, "Photo Motion"),
            (1, "main", 1),
        )

    def test_invalid_explicit_source_scene_number_is_not_hidden(self):
        row = {
            "part": 1,
            "role": "main",
            "local_scene": 2,
            "source_scene_number": 0,
            "scene": 2,
        }
        with self.assertRaises(RuntimeError):
            validate_final_output.scene_key(row, "Photo Motion")

    def test_hook_global_slot_does_not_shift_main_scene_identity(self):
        rows = [
            {
                "part": 1,
                "role": "hook",
                "scene_id": "part_01_hook",
                "scene": 1,
            },
            {
                "part": 1,
                "role": "main",
                "scene_id": "part_01_scene_01",
                "scene": 2,
                "local_scene": 2,
                "source_scene_number": 1,
            },
        ]
        mapping = validate_final_output.validate_role_rows(
            rows, [1], {1: 1}, "Photo Motion"
        )
        self.assertIn((1, "main", 1), mapping)

if __name__ == "__main__":
    unittest.main()
