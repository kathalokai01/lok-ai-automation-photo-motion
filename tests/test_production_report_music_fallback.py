import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import production_report


class EffectiveMusicReportTests(unittest.TestCase):
    def test_completed_local_fallback_overrides_external_unavailable_status(self):
        result = production_report.effective_music(
            {
                "status": "unavailable",
                "provider": "google_lyria",
                "model": "lyria-3.5",
                "reason": "External providers failed",
                "external_api_calls": 2,
                "paid_services_used": True,
                "paid_provider_api_calls": 2,
            },
            {
                "tracks": {
                    "music": {
                        "status": "completed",
                        "provider": "local_synthesis",
                        "generation": "local_deterministic_synthesis",
                    }
                }
            },
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["provider"], "local_synthesis")
        self.assertIsNone(result["model"])
        self.assertEqual(result["external_status"], "unavailable")
        self.assertEqual(result["external_provider"], "google_lyria")
        self.assertEqual(result["external_api_calls"], 2)

    def test_unavailable_without_local_track_remains_unavailable(self):
        result = production_report.effective_music(
            {
                "status": "unavailable",
                "provider": "google_lyria",
                "model": "lyria-3.5",
                "reason": "No provider succeeded",
            },
            {},
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["provider"], "google_lyria")
        self.assertEqual(result["model"], "lyria-3.5")
        self.assertEqual(result["reason"], "No provider succeeded")


if __name__ == "__main__":
    unittest.main()
