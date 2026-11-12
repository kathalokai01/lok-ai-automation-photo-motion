import unittest

from scripts.prepare_visuals import resolve_actual_scene_counts


class PrepareVisualsSceneMetadataTests(unittest.TestCase):
    def test_accepts_canonical_generate_scenes_part_role_counts(self):
        document = {
            "status": "completed",
            "part_role_counts": {
                "1": {"hook": 1, "main": 10, "resolution": 1, "suspense": 0}
            },
            "scenes": [],
        }
        self.assertEqual(resolve_actual_scene_counts(document, 1, 5, 20), {1: 10})

    def test_accepts_legacy_actual_scene_counts_list(self):
        document = {"actual_scene_counts": [10], "scenes_min": 5, "scenes_max": 20}
        self.assertEqual(resolve_actual_scene_counts(document, 1, 5, 20), {1: 10})

    def test_derives_counts_from_scene_rows_when_metadata_is_absent(self):
        document = {
            "scenes": [
                {"part": 1, "role": "hook"},
                *[{"part": 1, "role": "main", "scene": n} for n in range(1, 11)],
                {"part": 1, "role": "resolution"},
            ]
        }
        self.assertEqual(resolve_actual_scene_counts(document, 1, 5, 20), {1: 10})

    def test_rejects_missing_part_role_count(self):
        with self.assertRaisesRegex(RuntimeError, "missing Part 1"):
            resolve_actual_scene_counts({"part_role_counts": {}, "scenes": []}, 1, 5, 20)

    def test_rejects_counts_outside_configured_range(self):
        with self.assertRaisesRegex(RuntimeError, "within 5-9"):
            resolve_actual_scene_counts(
                {"part_role_counts": {"1": {"main": 10}}}, 1, 5, 9
            )

    def test_rejects_declared_range_mismatch_when_present(self):
        with self.assertRaisesRegex(RuntimeError, "minimum"):
            resolve_actual_scene_counts(
                {"part_role_counts": {"1": {"main": 10}}, "scenes_min": 4},
                1, 5, 20,
            )


if __name__ == "__main__":
    unittest.main()
