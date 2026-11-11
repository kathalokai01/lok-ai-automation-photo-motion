import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import music_provider_client as client


class StableAudioRequestTests(unittest.TestCase):
    def test_stable_audio_requests_audio_response_content(self):
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "audio/mpeg"
        response._content = b"x" * 12000

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "music.mp3"
            with patch.dict(os.environ, {
                "ALLOW_BILLABLE_PROVIDERS": "true",
                "MUSIC_ALLOW_PAID": "true",
                "STABILITY_API_KEY": "test-key",
                "STABLE_AUDIO_MODEL": "stable-audio-3",
            }), patch.object(client.requests, "post", return_value=response) as post:
                result = client.generate_music(
                    "stable_audio",
                    "Instrumental cinematic orchestral tension music",
                    30,
                    destination,
                )

            self.assertEqual(result, destination)
            self.assertTrue(destination.is_file())
            self.assertEqual(post.call_args.kwargs["headers"]["Accept"], "audio/*")


if __name__ == "__main__":
    unittest.main()
