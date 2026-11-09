#!/usr/bin/env python3
"""Regression tests for locked narration on extra scene roles."""

from __future__ import annotations

import re
import unittest

from scripts import generate_scenes as scenes


def ai_record(scene_id: str, role: str, narration: str) -> dict:
    narration = scenes.clean(narration)
    record = {
        "scene_id": scene_id,
        "role": role,
        "narration": narration,
        "visual_prompt": "A story-specific cinematic scene with the named characters and objects.",
        "source_text_sha256": scenes.text_sha256(narration),
    }
    if role == "main":
        record["source_word_count"] = len(re.findall(r"\S+", narration))
    else:
        record["voiceover_enabled"] = True
        record["narration_source"] = "ai_generated_role_narration"
    return record


class ExtraSceneNarrationTests(unittest.TestCase):
    def test_hook_and_resolution_keep_their_ai_narration(self) -> None:
        main_text = "राजू ने पुराने घर का दरवाज़ा धीरे से खोला।"
        hook_text = (
            "उस रात राजू ने घर के भीतर एक ऐसी आवाज़ सुनी "
            "जिसने उसकी पूरी ज़िंदगी बदल दी।"
        )
        resolution_text = (
            "सुबह होते ही राजू को उस रहस्य का सच पता चल गया "
            "और परिवार फिर से सुरक्षित हो गया।"
        )
        main = ai_record("part_01_scene_01", "main", main_text)
        hook = ai_record("part_01_hook", "hook", hook_text)
        resolution = ai_record(
            "part_01_resolution", "resolution", resolution_text
        )
        story = {
            "status": "completed",
            "format": "short",
            "total_parts": 1,
            "total_scenes": 1,
            "total_extra_scenes": 2,
            "parts": [{
                "part": 1,
                "narration": main_text,
                "text_locked": True,
                "locked_text_sha256": scenes.text_sha256(main_text),
                "scenes": [main],
                "hook_scene": hook,
                "suspense_scene": None,
                "resolution_scene": resolution,
            }],
        }

        parts, expected_items, main_total = scenes.validate_story(
            story,
            {"format": "short"},
            (1, 1),
        )

        self.assertEqual(len(parts), 1)
        self.assertEqual(main_total, 1)
        by_id = {item["scene_id"]: item for item in expected_items}
        self.assertEqual(by_id["part_01_hook"]["narration"], hook_text)
        self.assertEqual(
            by_id["part_01_resolution"]["narration"], resolution_text
        )

    def test_extra_scene_rejects_missing_narration(self) -> None:
        expected = {
            "part": 1,
            "scene": None,
            "scene_id": "part_01_hook",
            "role": "hook",
            "narration": "कम से कम आठ शब्दों का पूरा हुक वाक्य यहाँ होना चाहिए।",
            "source": {"story_anchor": "रात का रहस्य"},
        }
        invalid_scene = {
            "status": "completed",
            "part": 1,
            "scene": None,
            "scene_id": "part_01_hook",
            "role": "hook",
            "narration": "",
            "source_scene_id": "part_01_hook",
            "source_role": "hook",
            "source_scene_signature": scenes.expected_source_signature(expected),
            "generation_signature": "a" * 64,
            "scene_prompt_version": scenes.PROMPT_VERSION,
            "is_extra_scene": True,
            "visual_prompt": "A dark house at night.",
            "negative_prompt": "blurry, distorted",
            "camera_prompt": "slow push in",
            "lighting_prompt": "moonlight",
            "voiceover_enabled": True,
            "narration_source": "ai_generated_role_narration",
            "source_text_sha256": scenes.text_sha256(expected["narration"]),
        }

        self.assertFalse(
            scenes.scene_is_valid(invalid_scene, expected, "a" * 64)
        )


if __name__ == "__main__":
    unittest.main()
