#!/usr/bin/env python3
"""Regression tests for source-grounded Character Bible name extraction."""

from __future__ import annotations

import unittest

from scripts import generate_character_bible as bible


class CharacterNameExtractionTests(unittest.TestCase):
    def test_hindi_role_context_extracts_devanagari_names(self):
        source = (
            "न्यू-टेक सिटी के चीफ इंजीनियर अयान ने सिस्टम जाँचा। "
            "प्रोजेक्ट के मालिक विक्रांत मेहरा ने उद्घाटन रोका। "
            "अयान ने कोड सुधारा और बेटा रियान लिफ्ट में फँसा था।"
        )

        names = bible.extract_story_names(source)

        self.assertIn("अयान", names)
        self.assertIn("विक्रांत", names)
        self.assertIn("रियान", names)

    def test_repeated_english_proper_names_are_detected(self):
        source = (
            "Ayaan entered the server room. "
            "Vikrant Mehra left the tower. "
            "Ayaan restored Orion. "
            "Vikrant Mehra escaped."
        )

        names = bible.extract_story_names(source)

        self.assertIn("Ayaan", names)
        self.assertIn("Vikrant Mehra", names)

    def test_names_are_detected_across_scene_boundaries(self):
        rows = [
            {
                "part": 1,
                "scene": 1,
                "scene_id": "part_01_scene_01",
                "role": "main",
                "title": "",
                "narration": "चीफ इंजीनियर अयान ने सिस्टम की जाँच की।",
                "dialogue": "",
                "story_anchor": "",
                "beat": "",
                "purpose": "",
                "visual_prompt": "",
                "location": "",
                "time_period": "",
                "characters": [],
                "character_ids": [],
                "raw": {},
                "raw_records": [{}],
            },
            {
                "part": 1,
                "scene": 2,
                "scene_id": "part_01_scene_02",
                "role": "main",
                "title": "",
                "narration": "अयान ने आखिरी समय में नया कोड डाल दिया।",
                "dialogue": "",
                "story_anchor": "",
                "beat": "",
                "purpose": "",
                "visual_prompt": "",
                "location": "",
                "time_period": "",
                "characters": [],
                "character_ids": [],
                "raw": {},
                "raw_records": [{}],
            },
        ]

        found = bible.aggregate_characters(rows, {}, None)

        self.assertIn("अयान", found)
        self.assertGreater(found["अयान"]["mention_count"], 0)


class CharacterBibleFalsePositiveTests(unittest.TestCase):
    def test_prompt_directives_and_hindi_grammar_are_not_character_names(self):
        source = (
            "Preserve the character identity. Show the key event. "
            "Reveal the consequence. Bring the story to life. "
            "Create a cinematic frame. Deliver the scene. "
            "Establish continuity. Increase suspense. Introduce the setting. "
            "Pay attention to details. "
            "चीफ इंजीनियर अयान ने एआई सिस्टम की जाँच की। "
            "सिक्योरिटी हेड ने बताया कि बेटा रियान लिफ्ट में फँसा है।"
        )
        names = bible.extract_story_names(source)
        self.assertIn("अयान", names)
        self.assertIn("रियान", names)
        for false_name in (
            "Preserve", "Show", "Reveal", "Bring", "Create", "Deliver",
            "Establish", "Increase", "Introduce", "Pay", "का", "लिफ्ट",
        ):
            self.assertNotIn(false_name, names)
            self.assertFalse(bible.valid_name(false_name))

    def test_gender_markers_do_not_match_inside_ai_or_feminine_nouns(self):
        ambiguous_ayan = (
            "न्यू-टेक सिटी के चीफ इंजीनियर अयान को लॉन्च की रात "
            "अपने एआई सिस्टम में एक खतरनाक बग मिला।"
        )
        ambiguous_vikrant = (
            "ये रहे सारे दस्तावेज, विक्रांत ने ही सिस्टम के साथ "
            "छेड़छाड़ की थी।"
        )
        self.assertEqual(
            bible.nearby_gender_evidence(
                "अयान", ambiguous_ayan, "part_01_scene_01", ["अयान"]
            ),
            [],
        )
        self.assertEqual(
            bible.nearby_gender_evidence(
                "विक्रांत", ambiguous_vikrant, "part_01_scene_09", ["विक्रांत"]
            ),
            [],
        )
        male = "अयान दौड़ रहा था।"
        female = "सीमा दौड़ रही।"
        self.assertEqual(
            bible.nearby_gender_evidence(
                "अयान", male, "part_01_scene_02", ["अयान"]
            )[0]["value"],
            "male",
        )
        self.assertEqual(
            bible.nearby_gender_evidence(
                "सीमा", female, "part_01_scene_03", ["सीमा"]
            )[0]["value"],
            "female",
        )


if __name__ == "__main__":
    unittest.main()
