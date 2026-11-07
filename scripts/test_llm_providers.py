#!/usr/bin/env python3
"""Offline tests for zero-cost LLM provider selection and safety rules."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import llm_provider_client as client
import select_llm_provider as selector


class ProviderConfigurationTests(unittest.TestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_gemini_is_excluded_from_external_provider_order(self):
        os.environ["LLM_PROVIDER_ORDER"] = (
            "openrouter,gemini,groq,openrouter"
        )

        self.assertEqual(
            client.get_provider_order(),
            ["openrouter", "groq"],
        )

    def test_provider_order_preserves_configured_priority(self):
        os.environ["LLM_PROVIDER_ORDER"] = (
            "groq,openrouter,mistral"
        )

        self.assertEqual(
            client.get_provider_order(),
            ["groq", "openrouter", "mistral"],
        )

    def test_unsupported_provider_is_rejected(self):
        os.environ["LLM_PROVIDER_ORDER"] = "unknown-provider"

        with self.assertRaises(client.ProviderError):
            client.get_provider_order()

    def test_zero_cost_mode_is_mandatory(self):
        os.environ["LLM_ZERO_COST_ONLY"] = "false"
        os.environ["GROQ_API_KEY"] = "test-key"
        os.environ["GROQ_FREE_TIER_CONFIRMED"] = "true"

        with self.assertRaises(client.ProviderError):
            client.generate_text("groq", "test prompt")

    def test_openrouter_paid_model_is_rejected(self):
        os.environ["OPENROUTER_MODEL"] = "provider/model"

        with self.assertRaises(client.ProviderError):
            client.get_provider_model("openrouter")

    def test_openrouter_free_model_is_accepted(self):
        os.environ["OPENROUTER_MODEL"] = "provider/model:free"

        self.assertEqual(
            client.get_provider_model("openrouter"),
            "provider/model:free",
        )

    def test_missing_api_key_is_rejected(self):
        with self.assertRaises(client.ProviderError):
            client.get_provider_api_key("groq")

    def test_groq_requires_free_tier_confirmation(self):
        os.environ["GROQ_API_KEY"] = "test-key"
        os.environ["GROQ_MODEL"] = "test-model"
        os.environ["LLM_ZERO_COST_ONLY"] = "true"

        ready, model, detail = selector.validate_provider("groq")

        self.assertFalse(ready)
        self.assertEqual(model, "")
        self.assertIn("GROQ_FREE_TIER_CONFIRMED", detail)

    def test_groq_configuration_passes_local_checks(self):
        os.environ.update({
            "GROQ_API_KEY": "test-key",
            "GROQ_MODEL": "test-model",
            "GROQ_FREE_TIER_CONFIRMED": "true",
            "LLM_ZERO_COST_ONLY": "true",
        })

        ready, model, detail = selector.validate_provider("groq")

        self.assertTrue(ready)
        self.assertEqual(model, "test-model")
        self.assertTrue(detail)

    def test_openrouter_free_configuration_passes_local_checks(self):
        os.environ.update({
            "OPENROUTER_API_KEY": "test-key",
            "OPENROUTER_MODEL": "provider/model:free",
            "LLM_ZERO_COST_ONLY": "true",
        })

        ready, model, detail = selector.validate_provider("openrouter")

        self.assertTrue(ready)
        self.assertEqual(model, "provider/model:free")
        self.assertTrue(detail)

    def test_default_allowlist_excludes_cloudflare_and_mistral(self):
        self.assertEqual(
            selector.get_zero_cost_allowlist(),
            ["groq", "openrouter"],
        )

    def test_explicit_allowlist_preserves_order(self):
        os.environ["LLM_ZERO_COST_PROVIDERS"] = (
            "openrouter,groq,openrouter"
        )

        self.assertEqual(
            selector.get_zero_cost_allowlist(),
            ["openrouter", "groq"],
        )

    def test_gemini_cannot_be_added_as_external_provider(self):
        os.environ["LLM_ZERO_COST_PROVIDERS"] = "gemini"

        with self.assertRaises(client.ProviderError):
            selector.get_zero_cost_allowlist()

class ProviderResponseTests(unittest.TestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_openai_response_text_is_extracted(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": "Generated test story"
                    }
                }
            ]
        }

        self.assertEqual(
            client._extract_openai_text(response, "groq"),
            "Generated test story",
        )

    def test_empty_response_is_rejected(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": ""
                    }
                }
            ]
        }

        with self.assertRaises(client.ProviderError):
            client._extract_openai_text(response, "groq")

    def test_cloudflare_requires_correct_model_namespace(self):
        os.environ["CLOUDFLARE_MODEL"] = "invalid/model"

        with self.assertRaises(client.ProviderError):
            client.get_provider_model("cloudflare")

    def test_cloudflare_requires_account_id(self):
        os.environ["CLOUDFLARE_MODEL"] = "@cf/test-model"

        with self.assertRaises(client.ProviderError):
            client._cloudflare_url("@cf/test-model")

    def test_openrouter_generation_uses_mocked_request(self):
        os.environ.update({
            "LLM_ZERO_COST_ONLY": "true",
            "OPENROUTER_API_KEY": "test-key",
            "OPENROUTER_MODEL": "provider/model:free",
        })

        mocked_response = {
            "choices": [
                {
                    "message": {
                        "content": "Offline mocked response"
                    }
                }
            ],
            "usage": {
                "total_tokens": 12
            },
        }

        with patch.object(
            client,
            "_make_request",
            return_value=mocked_response,
        ) as mocked_request:

            result = client.generate_text(
                "openrouter",
                "Write a short test.",
            )

        mocked_request.assert_called_once()

        self.assertEqual(result["provider"], "openrouter")
        self.assertEqual(
            result["model"],
            "provider/model:free",
        )
        self.assertEqual(
            result["text"],
            "Offline mocked response",
        )
        self.assertEqual(
            result["usage"]["total_tokens"],
            12,
        )

    def test_paid_openrouter_override_is_blocked(self):
        os.environ.update({
            "LLM_ZERO_COST_ONLY": "true",
            "OPENROUTER_API_KEY": "test-key",
            "OPENROUTER_MODEL": "provider/model:free",
        })

        with patch.object(client, "_make_request") as mocked_request:
            with self.assertRaises(client.ProviderError):
                client.generate_text(
                    "openrouter",
                    "test prompt",
                    model="provider/paid-model",
                )

        mocked_request.assert_not_called()


class ProviderSelectorTests(unittest.TestCase):

    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_selector_disables_external_providers_without_zero_cost_mode(self):
        selection = selector.build_selection()

        self.assertEqual(selection["status"], "disabled")
        self.assertFalse(selection["enabled"])
        self.assertEqual(selection["provider_order"], [])

    def test_selector_returns_no_provider_when_credentials_are_missing(self):
        os.environ["LLM_ZERO_COST_ONLY"] = "true"

        selection = selector.build_selection()

        self.assertEqual(
            selection["status"],
            "no_provider_ready",
        )
        self.assertFalse(selection["enabled"])
        self.assertEqual(selection["provider_order"], [])

    def test_selector_preserves_ready_provider_order(self):
        os.environ.update({
            "LLM_ZERO_COST_ONLY": "true",
            "LLM_PROVIDER_ORDER": "openrouter,groq",
            "OPENROUTER_API_KEY": "test-openrouter-key",
            "OPENROUTER_MODEL": "provider/model:free",
            "GROQ_API_KEY": "test-groq-key",
            "GROQ_MODEL": "test-groq-model",
            "GROQ_FREE_TIER_CONFIRMED": "true",
        })

        selection = selector.build_selection()

        self.assertEqual(selection["status"], "ready")
        self.assertEqual(
            selection["provider_order"],
            ["openrouter", "groq"],
        )
        self.assertTrue(selection["gemini_first"])

    def test_selector_does_not_make_api_requests(self):
        os.environ.update({
            "LLM_ZERO_COST_ONLY": "true",
            "LLM_PROVIDER_ORDER": "openrouter",
            "OPENROUTER_API_KEY": "test-key",
            "OPENROUTER_MODEL": "provider/model:free",
        })

        with patch.object(
            client.urllib.request,
            "urlopen",
        ) as mocked_urlopen:
            selection = selector.build_selection()

        mocked_urlopen.assert_not_called()
        self.assertEqual(selection["provider_order"], ["openrouter"])


def run_tests() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__]
    )

    result = unittest.TextTestRunner(
        verbosity=2,
    ).run(suite)

    print("\nTEST SUMMARY")
    print("Total:", result.testsRun)
    print("Passed:", result.testsRun - len(result.failures) - len(result.errors))
    print("Failed:", len(result.failures))
    print("Errors:", len(result.errors))

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run_tests())