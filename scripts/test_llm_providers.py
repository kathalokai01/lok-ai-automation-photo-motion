#!/usr/bin/env python3
"""Offline tests for configured paid/free LLM provider selection and fallback."""

from __future__ import annotations

import os
import sys
import io
import urllib.error
from email.message import Message
from types import SimpleNamespace
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import llm_provider_client as client
import select_llm_provider as selector
import gemini_api_guard
import gemini_http
import generate_tts
import generate_ai_story
import select_gemini_model
import story_quality_validator


class GeminiHttpFallbackTests(unittest.TestCase):
    def _http_error(self, status, headers=None, body=b""):
        return urllib.error.HTTPError(
            "https://generativelanguage.googleapis.com/test",
            status,
            "test",
            headers or Message(),
            io.BytesIO(body),
        )

    def test_gemini_404_is_permanent_and_moves_to_next_model(self):
        error = self._http_error(404)
        self.assertEqual(gemini_http.http_status(error), 404)
        self.assertFalse(gemini_http.is_retryable_http_status(404))
        self.assertTrue(gemini_http.is_permanent_model_status(404))

    def test_gemini_429_is_retryable_not_model_permanent(self):
        self.assertTrue(gemini_http.is_retryable_http_status(429))
        self.assertFalse(gemini_http.is_permanent_model_status(429))

    def test_gemini_retry_after_header_is_honored(self):
        headers = Message()
        headers.add_header("Retry-After", "23")
        error = self._http_error(429, headers=headers)
        self.assertEqual(gemini_http.retry_after_seconds(error, 1), 23)

    def test_google_retry_info_is_honored(self):
        body = (
            b'{"error":{"details":[{"@type":"type.googleapis.com/'
            b'google.rpc.RetryInfo","retryDelay":"19s"}]}}'
        )
        error = self._http_error(429, body=body)
        self.assertEqual(gemini_http.retry_after_seconds(error, 1), 19)

    def test_retry_delay_is_bounded(self):
        headers = Message()
        headers.add_header("Retry-After", "900")
        error = self._http_error(429, headers=headers)
        self.assertEqual(gemini_http.retry_after_seconds(error, 1), 120)

    def test_retry_after_works_with_direct_requests_response(self):
        headers = Message()
        headers.add_header("Retry-After", "31")
        response = SimpleNamespace(
            status_code=429,
            headers=headers,
            text="Too many requests",
        )
        self.assertEqual(gemini_http.http_status(response), 429)
        self.assertEqual(gemini_http.retry_after_seconds(response, 1), 31)


class TTSFallbackHttpTests(unittest.TestCase):
    def _error(self, status, body="", retry_after=None):
        headers = Message()
        if retry_after is not None:
            headers.add_header("Retry-After", str(retry_after))
        return generate_tts.TTSProviderHTTPError(
            status, "test", body, headers
        )

    def test_tts_404_skips_unavailable_provider_without_blocking_paid_chain(self):
        error = self._error(404, "model not found")
        self.assertEqual(generate_tts.classify_provider_error(error), "model_unavailable")

    def test_tts_429_rate_limit_retries_same_provider(self):
        error = self._error(429, "Too many requests per minute", 17)
        self.assertEqual(generate_tts.classify_provider_error(error), "rate_limit")
        self.assertEqual(gemini_http.retry_after_seconds(error, 1), 17)

    def test_tts_429_daily_quota_blocks_only_that_paid_provider(self):
        error = self._error(429, "RESOURCE_EXHAUSTED: daily quota exceeded")
        self.assertEqual(generate_tts.classify_provider_error(error), "quota_or_billing")


class ProviderConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_gemini_is_excluded_from_external_provider_order(self):
        os.environ["LLM_PROVIDER_ORDER"] = "openrouter,gemini,groq,openrouter"
        self.assertEqual(client.get_provider_order(), ["openrouter", "groq"])

    def test_default_order_includes_every_supported_provider(self):
        self.assertEqual(client.get_provider_order(), list(client.SUPPORTED_PROVIDERS))

    def test_openai_is_first_default_provider(self):
        self.assertEqual(
            list(client.DEFAULT_PROVIDER_ORDER),
            ["openai", "groq", "openrouter", "cloudflare"],
        )
        self.assertEqual(client.PROVIDER_ENV["openai"]["key"], "OPENAI_API_KEY")
        self.assertEqual(client.PROVIDER_ENV["openai"]["model"], "OPENAI_MODEL")
        self.assertEqual(client.PROVIDER_ENV["openai"]["url"], "https://api.openai.com/v1/chat/completions")

    def test_configured_openai_provider_is_ready(self):
        os.environ.update({"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "gpt-6-luna"})
        ready, model, detail = selector.validate_provider("openai")
        self.assertTrue(ready)
        self.assertEqual(model, "gpt-6-luna")
        self.assertIn("present", detail)

    def test_openai_generation_uses_mocked_request(self):
        os.environ.update({"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "gpt-6-luna"})
        mocked_response = {"choices": [{"message": {"content": "OpenAI mocked response"}}], "usage": {"total_tokens": 9}}
        with patch.object(client, "_make_request", return_value=mocked_response) as mocked:
            result = client.generate_text("openai", "Write a short test.")
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.args[1], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["model"], "gpt-6-luna")
        self.assertEqual(result["text"], "OpenAI mocked response")

    def test_configured_priority_is_preserved_and_deduplicated(self):
        os.environ["LLM_PROVIDER_ORDER"] = "openai,groq,openrouter,cloudflare,openai"
        self.assertEqual(
            client.get_provider_order(),
            ["openai", "groq", "openrouter", "cloudflare"],
        )

    def test_gemini_in_provider_order_is_reserved_for_gemini_selector(self):
        os.environ["LLM_PROVIDER_ORDER"] = "gemini,groq,openrouter"
        self.assertEqual(selector.get_provider_allowlist(), ["groq", "openrouter"])
        self.assertEqual(client.get_provider_order(), ["groq", "openrouter"])

    def test_cloudflare_is_not_ready_without_account_id(self):
        os.environ.update({
            "CLOUDFLARE_API_TOKEN": "cf-token",
            "CLOUDFLARE_MODEL": "@cf/meta/test-model",
        })
        ready, model, detail = selector.validate_provider("cloudflare")
        self.assertFalse(ready)
        self.assertEqual(model, "")
        self.assertIn("CLOUDFLARE_ACCOUNT_ID", detail)

    def test_gemini_billing_guard_requires_exact_opt_in_and_key(self):
        with patch.dict(os.environ, {
            "ALLOW_GEMINI_GENERATION": "true",
            "GEMINI_API_KEY": "test-key",
        }, clear=True):
            self.assertFalse(gemini_api_guard.gemini_generation_allowed())
            with self.assertRaises(gemini_api_guard.GeminiBillingBlocked):
                gemini_api_guard.require_gemini_generation("unit test")

        with patch.dict(os.environ, {
            "ALLOW_GEMINI_GENERATION": "I_ACCEPT_POTENTIAL_GEMINI_CHARGES",
            "GEMINI_API_KEY": "test-key",
        }, clear=True):
            self.assertEqual(
                gemini_api_guard.require_gemini_generation("unit test"),
                "test-key",
            )

    def test_unsupported_provider_is_rejected(self):
        os.environ["LLM_PROVIDER_ORDER"] = "unknown-provider"
        with self.assertRaises(client.ProviderError):
            client.get_provider_order()

    def test_paid_openrouter_model_is_accepted(self):
        os.environ["OPENROUTER_MODEL"] = "provider/paid-model"
        self.assertEqual(client.get_provider_model("openrouter"), "provider/paid-model")

    def test_openrouter_free_model_is_also_accepted(self):
        os.environ["OPENROUTER_MODEL"] = "provider/model:free"
        self.assertEqual(client.get_provider_model("openrouter"), "provider/model:free")

    def test_missing_api_key_is_rejected(self):
        with self.assertRaises(client.ProviderError):
            client.get_provider_api_key("groq")

    def test_configured_groq_key_and_model_are_ready_without_free_confirmation(self):
        os.environ.update({"GROQ_API_KEY": "test-key", "GROQ_MODEL": "test-model"})
        ready, model, detail = selector.validate_provider("groq")
        self.assertTrue(ready)
        self.assertEqual(model, "test-model")
        self.assertIn("present", detail)

    def test_configured_cloudflare_is_supported_and_mistral_rejected(self):
        os.environ.update({
            "CLOUDFLARE_API_TOKEN": "cf-token",
            "CLOUDFLARE_ACCOUNT_ID": "account-id",
            "CLOUDFLARE_MODEL": "@cf/meta/test-model",
        })
        self.assertTrue(selector.validate_provider("cloudflare")[0])
        with self.assertRaises(client.ProviderError):
            client.get_provider_model("mistral")

    def test_selector_uses_all_configured_providers_without_cost_gate(self):
        os.environ.update({
            "LLM_PROVIDER_ORDER": "openai,groq",
            "OPENAI_API_KEY": "openai-key",
            "OPENAI_MODEL": "test-openai-model",
            "GROQ_API_KEY": "groq-key",
            "GROQ_MODEL": "llama-model",
        })
        selection = selector.build_selection()
        self.assertEqual(selection["status"], "ready")
        self.assertEqual(selection["provider_order"], ["openai", "groq"])
        self.assertTrue(selection["all_providers_enabled"])

    def test_selector_reports_no_provider_when_credentials_are_missing(self):
        selection = selector.build_selection()
        self.assertEqual(selection["status"], "no_provider_ready")
        self.assertFalse(selection["enabled"])
        self.assertEqual(selection["provider_order"], [])

    def test_selector_does_not_make_api_requests(self):
        os.environ.update({
            "LLM_PROVIDER_ORDER": "openrouter",
            "OPENROUTER_API_KEY": "test-key",
            "OPENROUTER_MODEL": "provider/paid-model",
        })
        with patch.object(client.urllib.request, "urlopen") as mocked_urlopen:
            selection = selector.build_selection()
        mocked_urlopen.assert_not_called()
        self.assertEqual(selection["provider_order"], ["openrouter"])

    def test_openrouter_paid_generation_uses_mocked_request(self):
        os.environ.update({
            "OPENROUTER_API_KEY": "test-key",
            "OPENROUTER_MODEL": "provider/paid-model",
        })
        mocked_response = {
            "choices": [{"message": {"content": "Offline mocked response"}}],
            "usage": {"total_tokens": 12},
        }
        with patch.object(client, "_make_request", return_value=mocked_response) as mocked:
            result = client.generate_text("openrouter", "Write a short test.")
        mocked.assert_called_once()
        self.assertEqual(result["provider"], "openrouter")
        self.assertEqual(result["model"], "provider/paid-model")
        self.assertEqual(result["text"], "Offline mocked response")
        self.assertEqual(result["usage"]["total_tokens"], 12)

    def test_explicit_model_override_is_used(self):
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        os.environ["OPENROUTER_MODEL"] = "provider/default-model"
        mocked_response = {"choices": [{"message": {"content": "ok"}}]}
        with patch.object(client, "_make_request", return_value=mocked_response) as mocked:
            result = client.generate_text(
                "openrouter", "test prompt", model="provider/paid-override"
            )
        self.assertEqual(result["model"], "provider/paid-override")
        self.assertEqual(mocked.call_args.args[1], "https://openrouter.ai/api/v1/chat/completions")


class ProviderResponseTests(unittest.TestCase):
    def test_main_scene_hash_uses_text_sha256(self):
        narration = "Locked source sentence."
        source_part = {
            "part": 1,
            "narration": narration,
            "scenes": [{
                "scene": 1,
                "scene_id": "part_01_scene_01",
                "story_anchor": narration,
                "narration": narration,
            }],
        }
        generated = [{
            "scene": 1,
            "scene_id": "part_01_scene_01",
            "role": "main",
            "narration": "Model narration must not replace locked source text.",
            "dialogue": "",
            "visual_prompt": "A cinematic scene.",
            "negative_prompt": "",
            "camera_prompt": "Slow camera movement.",
            "lighting_prompt": "Soft cinematic lighting.",
            "sfx_prompt": "",
            "music_prompt": "",
        }]

        records = generate_ai_story.validate_generated_scenes(
            generated,
            source_part,
            {"format": "full"},
            1,
        )

        self.assertEqual(records[0]["narration"], narration)
        self.assertEqual(
            records[0]["source_text_sha256"],
            generate_ai_story.text_sha256(narration),
        )
        self.assertNotEqual(
            records[0]["source_text_sha256"],
            generate_ai_story.stable_hash(narration),
        )

    def test_openai_response_text_is_extracted(self):
        response = {"choices": [{"message": {"content": "Generated test story"}}]}
        self.assertEqual(client._extract_openai_text(response, "groq"), "Generated test story")

    def test_empty_response_is_rejected(self):
        response = {"choices": [{"message": {"content": ""}}]}
        with self.assertRaises(client.ProviderError):
            client._extract_openai_text(response, "groq")

    def test_cloudflare_requires_correct_model_namespace(self):
        with patch.dict(os.environ, {"CLOUDFLARE_MODEL": "invalid/model"}, clear=True):
            with self.assertRaises(client.ProviderError):
                client.get_provider_model("cloudflare")

    def test_cloudflare_requires_account_id(self):
        with patch.dict(os.environ, {"CLOUDFLARE_MODEL": "@cf/test-model"}, clear=True):
            with self.assertRaises(client.ProviderError):
                client._cloudflare_url("@cf/test-model")



class AIStoryFallbackTests(unittest.TestCase):
    def setUp(self):
        self.source_part = {"part": 1, "scenes": []}
        self.config = {"format": "full"}

    def _patch_generation(self):
        return (
            patch.object(generate_ai_story, "build_part_prompt", return_value="prompt"),
            patch.object(generate_ai_story, "build_response_schema", return_value={}),
            patch.object(
                generate_ai_story,
                "validate_generated_part",
                return_value={"word_count": 130},
            ),
            patch.object(
                generate_ai_story,
                "validate_story_part",
                side_effect=lambda generated, source, total_parts: {
                    "version": story_quality_validator.VERSION,
                    "status": "passed",
                    "passed": True,
                    "score": 10,
                    "issues": [],
                    "source_part_signature": generate_ai_story.source_part_signature(source),
                },
            ),
        )

    def test_model_is_retried_three_times_before_switching_provider(self):
        models = ["gemini|gemini-test", "openai|openai-test"]
        failure = client.ProviderError(
            "gemini", "temporary network failure", retryable=True
        )
        request = [failure, failure, failure, {"text": "ok"}]
        patches = self._patch_generation()
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(generate_ai_story, "request_selected_model", side_effect=request) as mocked, \
             patch.object(generate_ai_story.time, "sleep") as sleep:
            result = generate_ai_story.generate_part(
                "", models, "topic", "story", self.source_part,
                self.config, 1,
            )
        self.assertEqual(mocked.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 30])
        self.assertEqual(
            [call.args[1] for call in mocked.call_args_list],
            ["gemini|gemini-test"] * 3 + ["openai|openai-test"],
        )
        self.assertEqual(result["_katha_model_index"], 1)
        self.assertEqual(result["_katha_provider_used"], "openai")

    def test_generation_starts_with_persisted_successful_model_position(self):
        models = ["gemini|first", "openai|second", "groq|third"]
        patches = self._patch_generation()
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(
                 generate_ai_story,
                 "request_selected_model",
                 return_value={"text": "ok"},
             ) as mocked:
            result = generate_ai_story.generate_part(
                "", models, "topic", "story", self.source_part,
                self.config, 1, start_model_index=1,
            )
        self.assertEqual(mocked.call_args.args[1], "openai|second")
        self.assertEqual(result["_katha_model_index"], 1)

    def test_all_model_failures_return_resumable_model_position(self):
        models = ["gemini|first", "openai|second"]
        failure = client.ProviderError("test", "permanent failure")
        patches = self._patch_generation()
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(
                 generate_ai_story,
                 "request_selected_model",
                 side_effect=failure,
             ):
            with self.assertRaises(generate_ai_story.ModelFallbackError) as caught:
                generate_ai_story.generate_part(
                    "", models, "topic", "story", self.source_part,
                    self.config, 1,
                )
        self.assertEqual(caught.exception.state["part"], 1)
        self.assertEqual(caught.exception.state["model_index"], 1)
        self.assertEqual(caught.exception.state["model"], "openai|second")
        self.assertEqual(caught.exception.state["max_attempts"], 3)
        self.assertEqual(caught.exception.state["retry_rounds"], 3)

    def test_quota_exhaustion_is_classified_for_immediate_provider_switch(self):
        failure = client.ProviderError(
            "gemini",
            "RESOURCE_EXHAUSTED: daily quota exceeded",
            status_code=429,
            retryable=True,
        )
        self.assertTrue(generate_ai_story.is_quota_exhausted(failure))


    def test_429_retry_after_is_honored_before_same_model_retry(self):
        headers = Message()
        headers.add_header("Retry-After", "41")
        failure = client.ProviderError(
            "gemini", "temporary rate limit", status_code=429,
            retryable=True, headers=headers,
        )
        models = ["gemini|first", "openai|second"]
        patches = self._patch_generation()
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(
                 generate_ai_story,
                 "request_selected_model",
                 side_effect=[failure, failure, failure, {"text": "ok"}],
             ), \
             patch.object(generate_ai_story.time, "sleep") as sleep:
            generate_ai_story.generate_part(
                "", models, "topic", "story", self.source_part,
                self.config, 1,
            )
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [41, 41])

    def test_quota_exhaustion_switches_without_retry_wait(self):
        quota = client.ProviderError(
            "gemini", "RESOURCE_EXHAUSTED: daily quota exceeded",
            status_code=429, retryable=True,
        )
        models = ["gemini|first", "openai|second"]
        patches = self._patch_generation()
        with patches[0], patches[1], patches[2], \
             patch.object(
                 generate_ai_story,
                 "request_selected_model",
                 side_effect=[quota, {"text": "ok"}],
             ) as request, \
             patch.object(generate_ai_story.time, "sleep") as sleep:
            generate_ai_story.generate_part(
                "", models, "topic", "story", self.source_part,
                self.config, 1,
            )
        self.assertEqual(request.call_count, 2)
        sleep.assert_not_called()



class StoryProgressionAndQualityTests(unittest.TestCase):
    def test_story_cursor_round_trips_only_for_same_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            models = ["gemini|gemini-a", "openai|model-b"]
            with patch.object(select_gemini_model, "STORY_PROGRESSION_FILE", path):
                self.assertEqual(select_gemini_model.load_story_model_cursor(models), 0)
                select_gemini_model.save_story_model_cursor(
                    models, 1, stage="test", part=2,
                )
                self.assertEqual(select_gemini_model.load_story_model_cursor(models), 1)
                self.assertEqual(
                    select_gemini_model.load_story_model_cursor(
                        ["openai|model-b", "gemini|gemini-a"]
                    ),
                    0,
                )

    def test_selected_provider_candidates_preserve_ready_order(self):
        selection = {
            "enabled": True, "status": "ready",
            "provider_order": ["groq", "openai"],
            "providers": [
                {"provider": "openai", "model": "gpt-test", "ready": True},
                {"provider": "groq", "model": "llama-test", "ready": True},
            ],
        }
        self.assertEqual(
            client.selected_story_provider_candidates(selection),
            ["groq|llama-test", "openai|gpt-test"],
        )

    def test_story_quality_validator_accepts_locked_unique_scene(self):
        source = {
            "part": 1,
            "scenes": [{
                "scene": 1, "scene_id": "P01-S01",
                "story_anchor": "राम जंगल के रास्ते घर लौट रहा था।",
            }],
        }
        generated = {
            "part": 1, "status": "completed", "story_title": "जंगल का रास्ता",
            "source_part_signature": "a" * 64,
            "scenes": [{
                "scene": 1, "scene_id": "P01-S01",
                "story_anchor": "राम जंगल के रास्ते घर लौट रहा था।",
                "narration": "राम जंगल के रास्ते घर लौट रहा था।",
                "role": "main", "status": "completed",
                "visual_prompt": "A detailed cinematic forest path at dusk with a lone traveler returning home.",
                "camera_prompt": "slow push in",
                "lighting_prompt": "natural dusk lighting",
            }],
        }
        report = story_quality_validator.validate_story_part(
            generated, source, total_parts=1,
        )
        self.assertTrue(report["passed"])
        self.assertEqual(report["score"], 10)

    def test_generation_failure_classifier_distinguishes_quota(self):
        error = urllib.error.HTTPError(
            "https://example.invalid", 429, "quota",
            Message(), io.BytesIO(b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'),
        )
        self.assertEqual(
            gemini_http.classify_generation_failure(error), "quota_blocked"
        )


def run_tests() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\nTEST SUMMARY")
    print("Total:", result.testsRun)
    print("Passed:", result.testsRun - len(result.failures) - len(result.errors))
    print("Failed:", len(result.failures))
    print("Errors:", len(result.errors))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run_tests())
