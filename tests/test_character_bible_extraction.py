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


if __name__ == "__main__":
    unittest.main()
