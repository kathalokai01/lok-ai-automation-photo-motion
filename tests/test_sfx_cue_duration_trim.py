import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_sfx_timing import validate_cue_file_duration


class SfxCueDurationTests(unittest.TestCase):
    def test_shorter_declared_cue_is_valid_trim_of_longer_source(self):
        declared, measured = validate_cue_file_duration(1.75, 8.0, "part_01_hook")
        self.assertEqual(declared, 1.75)
        self.assertEqual(measured, 8.0)

    def test_equal_declared_and_measured_duration_is_valid(self):
        declared, measured = validate_cue_file_duration(8.0, 8.0, "part_01_scene_01")
        self.assertEqual(declared, measured)

    def test_declared_duration_cannot_exceed_source_file(self):
        with self.assertRaisesRegex(RuntimeError, "exceeds measured file duration"):
            validate_cue_file_duration(8.5, 8.0, "part_01_hook")

    def test_nonpositive_duration_is_rejected(self):
        with self.assertRaises(RuntimeError):
            validate_cue_file_duration(0.0, 8.0, "part_01_hook")


if __name__ == "__main__":
    unittest.main()
