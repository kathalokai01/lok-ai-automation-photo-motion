"""Production event journal and pipeline-preflight wrapper regression tests."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pipeline_preflight
import production_events


class EventTests(unittest.TestCase):
    def test_event_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(production_events, "EVENTS", root / "events.jsonl"),
                patch.object(production_events, "STATE", root / "state.json"),
            ):
                production_events.record_event("render", "completed")
                summary = production_events.stage_summary()

        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["stage_count"], 1)

    def test_preflight_wrapper_records_success_without_removed_config_api(self):
        with (
            patch.object(pipeline_preflight, "_photo_motion_original_main", return_value=0),
            patch.object(production_events, "record_event") as record,
        ):
            result = pipeline_preflight.main()

        self.assertEqual(result, 0)
        self.assertEqual(record.call_count, 2)
        self.assertEqual(record.call_args_list[0].args, ("pipeline_preflight", "running"))
        self.assertEqual(record.call_args_list[1].args, ("pipeline_preflight", "completed"))

    def test_preflight_wrapper_records_failure_and_reraises(self):
        failure = RuntimeError("preflight test failure")
        with (
            patch.object(pipeline_preflight, "_photo_motion_original_main", side_effect=failure),
            patch.object(production_events, "record_event") as record,
        ):
            with self.assertRaisesRegex(RuntimeError, "preflight test failure"):
                pipeline_preflight.main()

        self.assertEqual(record.call_count, 2)
        self.assertEqual(record.call_args_list[1].args, ("pipeline_preflight", "failed"))
        self.assertIn("preflight test failure", record.call_args_list[1].kwargs["error"])


if __name__ == "__main__":
    unittest.main()
