import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import production_report


class ProductionReportSoundGateTests(unittest.TestCase):
    def setUp(self):
        self.config = {"MUSIC": "true", "SFX": "true"}
        self.music = {"status": "completed", "provider": "local_synthesis"}
        self.sound = {
            "status": "completed",
            "sfx_cues": [{"scene_id": "part_01_scene_01"}],
            "tracks": {
                "music": {"status": "completed", "provider": "local_synthesis"},
                "ambient": {"status": "completed"},
            },
        }

    def test_nested_sfx_validation_schema_passes_with_local_music_fallback(self):
        validation = {
            "status": "completed",
            "total_visual_scene_records": 12,
            "sfx": {
                "status": "completed",
                "cue_count": 12,
                "scenes_without_sfx": 0,
            },
        }
        self.assertTrue(production_report.evaluate_sound_checks(
            self.config, self.music, self.sound, validation
        ))
        summary = production_report.sound_quality_summary(self.sound, validation)
        self.assertEqual(summary["sfx_cue_count"], 12)
        self.assertEqual(summary["sfx_scenes_without_cues"], 0)
        self.assertEqual(summary["sfx_validation_status"], "completed")

    def test_sfx_missing_scene_coverage_fails(self):
        validation = {
            "status": "completed",
            "sfx": {
                "status": "completed",
                "cue_count": 12,
                "scenes_without_sfx": 1,
            },
        }
        self.assertFalse(production_report.evaluate_sound_checks(
            self.config, self.music, self.sound, validation
        ))

    def test_missing_sfx_coverage_metadata_fails_closed(self):
        validation = {
            "status": "completed",
            "sfx": {"status": "completed", "cue_count": 12},
        }
        self.assertFalse(production_report.evaluate_sound_checks(
            self.config, self.music, self.sound, validation
        ))

    def test_failed_sfx_timing_check_fails(self):
        validation = {
            "status": "completed",
            "sfx": {
                "status": "completed",
                "cue_count": 12,
                "scenes_without_sfx": 0,
                "timing_errors": 1,
            },
        }
        self.assertFalse(production_report.evaluate_sound_checks(
            self.config, self.music, self.sound, validation
        ))

    def test_disabled_sfx_does_not_block_when_music_is_complete(self):
        config = {"MUSIC": "true", "SFX": "false"}
        self.assertTrue(production_report.evaluate_sound_checks(
            config, self.music, self.sound, {"status": "missing"}
        ))

    def test_music_unavailable_without_fallback_still_fails(self):
        music = {"status": "unavailable"}
        validation = {
            "status": "completed",
            "sfx": {"status": "completed", "cue_count": 12, "scenes_without_sfx": 0},
        }
        self.assertFalse(production_report.evaluate_sound_checks(
            self.config, music, self.sound, validation
        ))


if __name__ == "__main__":
    unittest.main()
