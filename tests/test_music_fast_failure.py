from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FLOW = (ROOT / "scripts" / "generate_flow_music.py").read_text(encoding="utf-8")
CLIENT = (ROOT / "scripts" / "music_provider_client.py").read_text(encoding="utf-8")


class MusicFastFailureTests(unittest.TestCase):
    def test_network_timeouts_are_bounded(self):
        self.assertIn("REQUEST_TIMEOUT = (10, 90)", FLOW)
        self.assertIn("TIMEOUT = (10, 90)", CLIENT)

    def test_lyria_retry_budget_is_bounded(self):
        self.assertIn("RETRY_DELAYS = (3,)", FLOW)

    def test_permanent_provider_failure_opens_circuit(self):
        self.assertIn("blocked_providers = set()", FLOW)
        self.assertIn("blocked_providers.add(provider)", FLOW)
        self.assertIn("MUSIC PROVIDER CIRCUIT OPEN; SKIPPING:", FLOW)

    def test_same_story_failure_is_cooled_down(self):
        self.assertIn('"retry_after_unix": time.time() + 21600', FLOW)
        self.assertIn('previous_failure.get("source_fingerprint") == source_sig', FLOW)
        self.assertIn('os.getenv("MUSIC_FORCE_RETRY", "false")', FLOW)


if __name__ == "__main__":
    unittest.main()
