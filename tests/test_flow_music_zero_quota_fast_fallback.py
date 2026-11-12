import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_flow_music


class LyriaZeroQuotaFastFallbackTests(unittest.TestCase):
    def test_zero_free_tier_limit_is_non_retryable(self):
        message = (
            "Rate limit exceeded for model lyria-3-pro "
            "(limit: 0 input tokens per minute on Free Tier)"
        )
        self.assertTrue(generate_flow_music.is_zero_quota_limit(message))

    def test_nonzero_rate_limit_remains_retryable(self):
        message = "Rate limit exceeded (limit: 10 requests per minute)"
        self.assertFalse(generate_flow_music.is_zero_quota_limit(message))

    def test_unrelated_error_is_not_classified_as_zero_quota(self):
        self.assertFalse(
            generate_flow_music.is_zero_quota_limit(
                "HTTP 503: temporary service unavailable"
            )
        )


if __name__ == "__main__":
    unittest.main()
