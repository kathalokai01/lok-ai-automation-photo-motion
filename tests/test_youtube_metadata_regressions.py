import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_youtube_metadata as metadata


class YouTubeMetadataRegressionTests(unittest.TestCase):
    def test_metadata_validation_uses_response_title_without_undefined_story(self):
        result = metadata.validate_metadata(
            {
                "title": "कहानी का सही शीर्षक",
                "description": "पूरी कहानी पर आधारित विवरण।",
                "tags": [
                    "हिंदी कहानी",
                    "रोमांचक कहानी",
                    "नैतिक कहानी",
                    "Katha Lok AI",
                    "Hindi story",
                ],
            },
            topic="कहानी",
            model="test-model",
            fingerprint="a" * 64,
            mode="short",
            provider="openai",
        )
        self.assertEqual(result["title"], "कहानी का सही शीर्षक")
        self.assertEqual(result["provider"], "openai")

    def test_canonical_story_title_overrides_model_generated_seo_title(self):
        generated = {"title": "अलग SEO शीर्षक #Shorts"}
        result = metadata.enforce_canonical_story_title(
            generated,
            {"existing_title": "लालच का टावर और कुदरत का न्याय"},
        )
        self.assertEqual(result["title"], "लालच का टावर और कुदरत का न्याय")

    def test_canonical_story_title_is_required(self):
        with self.assertRaisesRegex(RuntimeError, "Canonical story title is missing"):
            metadata.enforce_canonical_story_title({"title": "कुछ भी"}, {})

    def test_prompt_forbids_rewriting_canonical_story_title(self):
        prompt = metadata.build_prompt(
            "कहानी",
            {
                "existing_title": "लालच का टावर",
                "summary": "लालच के परिणाम",
                "text": "यह पूरी कहानी का पर्याप्त उदाहरण पाठ है। " * 10,
            },
            "short",
        )
        self.assertIn("#Shorts या अतिरिक्त SEO शब्द Title में न जोड़ें", prompt)

    def test_metadata_validator_has_no_undefined_story_reference(self):
        import inspect
        source = inspect.getsource(metadata.validate_metadata)
        self.assertNotIn("story.get(", source)

    def test_openai_provider_uses_completion_token_parameter(self):
        source = (ROOT / "scripts" / "llm_provider_client.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('if provider == "openai":', source)
        self.assertIn('payload["max_completion_tokens"] = int(max_tokens)', source)
        self.assertIn('payload["max_tokens"] = int(max_tokens)', source)

    def test_generated_metadata_still_requires_complete_story_prompt(self):
        source = (ROOT / "scripts" / "generate_youtube_metadata.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("COMPLETE STORY:", source)
        self.assertIn('story["text"]', source)
