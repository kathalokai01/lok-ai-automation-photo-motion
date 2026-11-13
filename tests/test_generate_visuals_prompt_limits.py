#!/usr/bin/env python3
"""Regression tests for bounded, story-preserving visual prompt construction."""

from __future__ import annotations

import unittest

from scripts import generate_visuals as visuals


class VisualPromptLimitTests(unittest.TestCase):
    def test_character_context_is_bounded_and_keeps_all_names(self):
        characters = [
            {
                "id": f"character_{index:02d}",
                "name": f"किरदार {index:02d}",
                "gender": "unspecified",
                "visual_identity": "चेहरे और पहनावे का विवरण " * 500,
                "consistency_rules": ["पहचान स्थिर रखें " * 300] * 8,
                "description": "लंबा विवरण " * 500,
            }
            for index in range(1, 16)
        ]

        compact = visuals.compact_character_context(characters)
        encoded = visuals.json.dumps(
            compact, ensure_ascii=False, separators=(",", ":")
        )

        self.assertLessEqual(len(encoded), 6500)
        self.assertEqual(len(compact), 15)
        for index in range(1, 16):
            self.assertIn(f"किरदार {index:02d}", encoded)

    def test_supplemental_scene_metadata_is_bounded(self):
        source = {"key_event": "महत्वपूर्ण घटना " * 5000}
        value = visuals.scene_detail(source, "key_event")
        self.assertLessEqual(len(value), 1400)
        self.assertTrue(value.startswith("महत्वपूर्ण घटना"))

    def test_optional_context_is_removed_before_locked_scene_text(self):
        visual = "EXACT STORY EVENT: " + ("मूल दृश्य निर्देश " * 100)
        narration = "LOCKED NARRATION: " + ("मूल कथन " * 100)
        sections = [
            visual,
            narration,
            "CHRONOLOGICAL CONTINUITY: " + ("पुराना संदर्भ " * 5000),
            "WORLD CONTINUITY LOCK: " + ("दुनिया संदर्भ " * 5000),
        ]

        prompt = visuals.fit_prompt_sections(sections, "part_01_hook")

        self.assertLessEqual(len(prompt), visuals.MAX_PROMPT_LENGTH)
        self.assertIn(visual, prompt)
        self.assertIn(narration, prompt)
        self.assertNotIn("CHRONOLOGICAL CONTINUITY:", prompt)
        self.assertNotIn("WORLD CONTINUITY LOCK:", prompt)

    def test_oversized_core_prompt_fails_without_silent_truncation(self):
        sections = [
            "EXACT STORY EVENT: " + ("दृश्य " * 15000),
            "LOCKED NARRATION: " + ("कथन " * 15000),
        ]
        with self.assertRaisesRegex(RuntimeError, "core visual instruction"):
            visuals.fit_prompt_sections(sections, "part_01_hook")


if __name__ == "__main__":
    unittest.main()
