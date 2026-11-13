#!/usr/bin/env python3
"""Regression tests for TTS provider API/model compatibility fixes."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import generate_tts as tts


class TTSProviderRegressionTests(unittest.TestCase):
    def test_edge_tts_rate_is_passed_as_single_cli_argument(self) -> None:
        with (
            patch.object(tts.shutil, "which", return_value="/usr/bin/edge-tts"),
            patch.object(tts, "run_command") as run_command,
            patch.object(
                tts, "validate_audio",
                return_value={"duration": 1.0, "size_bytes": 1000, "sha256": "a" * 64},
            ),
            patch.object(tts.os, "replace"),
        ):
            tts.synthesize_provider(
                "edge_tts", "नमस्ते।", Path("/tmp/edge-test.mp3"),
                {}, "+0%", "female", {},
            )

        command = run_command.call_args.args[0]
        self.assertIn("--rate=+0%", [str(value) for value in command])
        self.assertNotIn("--rate", [str(value) for value in command])

    def test_cartesia_retries_sunsetted_model_with_sonic_latest(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "CARTESIA_API_KEY": "test-key",
                    "CARTESIA_VOICE_ID": "test-voice",
                    "CARTESIA_TTS_MODEL": "sonic-2",
                },
                clear=True,
            ),
            patch.object(
                tts, "request_bytes",
                side_effect=[
                    RuntimeError("Model sunsetted: use sonic-latest instead"),
                    (b"audio-bytes", "audio/mpeg"),
                ],
            ) as request_bytes,
            patch.object(
                tts, "write_audio_bytes",
                return_value={"duration": 1.0, "size_bytes": 1000, "sha256": "b" * 64},
            ) as write_audio,
        ):
            result = tts.synthesize_provider(
                "cartesia", "नमस्ते।", Path("/tmp/cartesia-test.mp3"),
                {}, "+0%", "female",
            )

        self.assertEqual(result["duration"], 1.0)
        self.assertEqual(request_bytes.call_count, 2)
        self.assertEqual(
            request_bytes.call_args_list[1].args[2]["model_id"],
            "sonic-latest",
        )
        write_audio.assert_called_once()

    def test_finevoice_reads_nested_task_and_audio_url_fields(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "FINEVOICE_API_KEY": "test-key",
                    "FINEVOICE_TTS_URL": "https://apis.finevoice.ai/v1/audio/speech-synthesis",
                    "FINEVOICE_VOICE_ID": "hindi-narrator",
                },
                clear=True,
            ),
            patch.object(
                tts, "request_json",
                side_effect=[
                    {"status": 202, "data": {"task_id": "nested-task-1"}},
                    {"status": 200, "result": {"audio_url": "https://cdn.finevoice.ai/nested.mp3"}},
                ],
            ) as request_json,
            patch.object(
                tts, "request_bytes",
                return_value=(b"audio-bytes", "audio/mpeg"),
            ) as request_bytes,
            patch.object(
                tts, "write_audio_bytes",
                return_value={"duration": 1.0, "size_bytes": 1000, "sha256": "c" * 64},
            ),
            patch.object(tts.time, "sleep"),
        ):
            result = tts.synthesize_provider(
                "finevoice", "नमस्ते।", Path("/tmp/finevoice-test.mp3"),
                {}, "+0%", "female",
            )

        self.assertEqual(result["duration"], 1.0)
        self.assertIn("/v1/task/nested-task-1", request_json.call_args_list[1].args[0])
        request_bytes.assert_called_once()
        self.assertEqual(
            request_bytes.call_args.args[0],
            "https://cdn.finevoice.ai/nested.mp3",
        )

    def test_minimax_provider_error_is_preserved_in_failure(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "MINIMAX_API_KEY": "test-key",
                    "MINIMAX_TTS_VOICE_ID": "test-voice",
                },
                clear=True,
            ),
            patch.object(
                tts, "request_json",
                return_value={
                    "base_resp": {"status_code": 1002, "status_msg": "invalid voice"},
                    "data": {},
                },
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "invalid voice"
            ):
                tts.synthesize_provider(
                    "minimax", "नमस्ते।", Path("/tmp/minimax-test.mp3"),
                    {}, "+0%", "female",
                )


if __name__ == "__main__":
    unittest.main()
