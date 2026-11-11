#!/usr/bin/env python3
"""FineVoice asynchronous TTS adapter regression tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from scripts import generate_tts as tts


class FineVoiceAsyncTTSTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "FINEVOICE_API_KEY": "test-api-key",
                "FINEVOICE_TTS_URL": (
                    "https://apis.finevoice.ai/v1/audio/speech-synthesis"
                ),
                "FINEVOICE_VOICE_ID": "hindi_narrator",
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_submits_polls_and_downloads_audio(self) -> None:
        submitted = {
            "status": 202,
            "taskId": "task-123",
        }
        completed = {
            "status": 200,
            "url": "https://cdn.finevoice.ai/audio/result.mp3",
        }

        with (
            patch.object(
                tts, "request_json",
                side_effect=[submitted, completed],
            ) as request_json,
            patch.object(
                tts, "request_bytes",
                return_value=(b"verified-audio-bytes", "audio/mpeg"),
            ) as request_bytes,
            patch.object(
                tts, "write_audio_bytes",
                return_value={
                    "duration": 2.5,
                    "size_bytes": 1000,
                    "sha256": "a" * 64,
                },
            ) as write_audio,
            patch.object(tts.time, "sleep") as sleep,
        ):
            result = tts.synthesize_provider(
                "finevoice",
                "नमस्ते दुनिया।",
                Path("finevoice-test.mp3"),
                {},
                "+0%",
                "female",
            )

        self.assertEqual(result["duration"], 2.5)
        self.assertEqual(request_json.call_count, 2)
        submit_call = request_json.call_args_list[0]
        self.assertEqual(
            submit_call.args[0],
            "https://apis.finevoice.ai/v1/audio/speech-synthesis",
        )
        self.assertEqual(
            submit_call.args[2],
            {
                "voice": "hindi_narrator",
                "text": "नमस्ते दुनिया।",
            },
        )
        self.assertEqual(submit_call.kwargs["method"], "POST")

        poll_call = request_json.call_args_list[1]
        self.assertEqual(
            poll_call.args[0],
            "https://apis.finevoice.ai/v1/task/task-123",
        )
        self.assertEqual(poll_call.kwargs["method"], "GET")
        sleep.assert_called_once_with(2)

        request_bytes.assert_called_once_with(
            "https://cdn.finevoice.ai/audio/result.mp3",
            headers={},
            payload=None,
            method="GET",
            timeout=tts.REQUEST_TIMEOUT,
        )
        write_audio.assert_called_once()
        self.assertEqual(
            write_audio.call_args.args[1],
            b"verified-audio-bytes",
        )

    def test_accepts_immediate_result_url_without_polling(self) -> None:
        with (
            patch.object(
                tts,
                "request_json",
                return_value={
                    "status": 200,
                    "url": "https://cdn.finevoice.ai/audio/ready.mp3",
                },
            ) as request_json,
            patch.object(
                tts,
                "request_bytes",
                return_value=(b"audio", "audio/mpeg"),
            ) as request_bytes,
            patch.object(
                tts,
                "write_audio_bytes",
                return_value={"duration": 1.0},
            ) as write_audio,
        ):
            result = tts.synthesize_provider(
                "finevoice",
                "परीक्षण।",
                Path("finevoice-ready.mp3"),
                {},
                "+0%",
                "female",
            )

        self.assertEqual(result["duration"], 1.0)
        request_json.assert_called_once()
        request_bytes.assert_called_once()
        write_audio.assert_called_once()

    def test_rejects_non_https_audio_download_url(self) -> None:
        with (
            patch.object(
                tts,
                "request_json",
                return_value={
                    "status": 200,
                    "url": "http://example.invalid/audio.mp3",
                },
            ),
            patch.object(tts, "request_bytes") as request_bytes,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "invalid HTTPS audio URL"
            ):
                tts.synthesize_provider(
                    "finevoice",
                    "परीक्षण।",
                    Path("finevoice-invalid.mp3"),
                    {},
                    "+0%",
                    "female",
                )

        request_bytes.assert_not_called()

    def test_fails_fast_when_task_reports_failure(self) -> None:
        with (
            patch.object(
                tts,
                "request_json",
                side_effect=[
                    {"status": 202, "taskId": "task-failed"},
                    {
                        "status": "failed",
                        "error": {"message": "voice unavailable"},
                    },
                ],
            ),
            patch.object(tts.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "FineVoice TTS task failed"
            ):
                tts.synthesize_provider(
                    "finevoice",
                    "परीक्षण।",
                    Path("finevoice-failed.mp3"),
                    {},
                    "+0%",
                    "female",
                )


if __name__ == "__main__":
    unittest.main()
