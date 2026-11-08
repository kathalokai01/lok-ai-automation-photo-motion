#!/usr/bin/env python3
"""Offline tests for configured paid/free LLM provider selection and fallback."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import llm_provider_client as client
import select_llm_provider as selector
import gemini_api_guard


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

    def test_configured_priority_is_preserved_and_deduplicated(self):
        os.environ["LLM_PROVIDER_ORDER"] = "mistral,openrouter,groq,mistral"
        self.assertEqual(client.get_provider_order(), ["mistral", "openrouter", "groq"])

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

    def test_configured_cloudflare_and_mistral_are_supported(self):
        os.environ.update({
            "CLOUDFLARE_API_TOKEN": "cf-token",
            "CLOUDFLARE_ACCOUNT_ID": "account-id",
            "CLOUDFLARE_MODEL": "@cf/meta/test-model",
            "MISTRAL_API_KEY": "mistral-key",
            "MISTRAL_MODEL": "mistral-large-latest",
        })
        self.assertTrue(selector.validate_provider("cloudflare")[0])
        self.assertTrue(selector.validate_provider("mistral")[0])

    def test_selector_uses_all_configured_providers_without_zero_cost_flag(self):
        os.environ.update({
            "LLM_PROVIDER_ORDER": "mistral,groq",
            "MISTRAL_API_KEY": "mistral-key",
            "MISTRAL_MODEL": "mistral-large-latest",
            "GROQ_API_KEY": "groq-key",
            "GROQ_MODEL": "llama-model",
        })
        selection = selector.build_selection()
        self.assertEqual(selection["status"], "ready")
        self.assertEqual(selection["provider_order"], ["mistral", "groq"])
        self.assertTrue(selection["all_providers_enabled"])
        self.assertNotIn("zero_cost_only", selection)

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
