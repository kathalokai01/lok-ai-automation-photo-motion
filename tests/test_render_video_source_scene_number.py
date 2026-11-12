import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render_video


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


if __name__ == "__main__":
    unittest.main()
