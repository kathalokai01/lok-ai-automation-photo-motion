#!/usr/bin/env python3
"""Regression tests for locked narration on extra scene roles."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts import checkpoint_manager

from scripts import generate_scenes as scenes
from scripts import generate_narration as narration
from scripts import generate_story as story


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


    def test_scene_progress_checkpoint_is_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "output"
            checkpoint = source / "checkpoints" / "scenes_progress.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text(
                json.dumps({
                    "version": "photo-motion-scenes-v11-extra-role-narration-fix",
                    "status": "in_progress",
                    "generation_signature": "b" * 64,
                    "completed_scene_ids": ["part_01_scene_01"],
                }),
                encoding="utf-8",
            )

            copied = checkpoint_manager.merge_progress_checkpoints(
                source, output
            )

            restored = output / "checkpoints" / "scenes_progress.json"
            self.assertEqual(copied, 1)
            self.assertTrue(restored.is_file())
            self.assertEqual(
                json.loads(restored.read_text(encoding="utf-8"))[
                    "generation_signature"
                ],
                "b" * 64,
            )



    def test_story_scene_and_narration_use_identical_word_count(self) -> None:
        # An isolated em dash is a whitespace token in Story partitioning.
        # Regex word-boundary counting previously discarded it in narration.
        sample = "राजू ने कहा — ‘रुको!’ फिर वह लौट आया।"
        expected = len(re.findall(r"\S+", re.sub(r"\s+", " ", sample).strip()))
        self.assertEqual(story.word_count(sample), expected)
        self.assertEqual(scenes.count_narration_words(sample), expected)
        self.assertEqual(narration.count_words(sample), expected)

    def test_extra_scene_word_count_uses_narration_validator_rule(self) -> None:
        # All pipeline stages now use whitespace-token counting.
        sample = "राजू ने कहा — ‘रुको!’ फिर वह लौट आया।"
        expected = narration.count_words(sample)
        self.assertEqual(scenes.count_narration_words(sample), expected)

        extra = {
            "part": 1,
            "scene": None,
            "scene_id": "part_01_hook",
            "role": "hook",
            "narration": sample,
            "source": {"story_anchor": "रात का रहस्य"},
        }
        record = {
            "status": "completed",
            "part": 1,
            "scene": None,
            "scene_id": "part_01_hook",
            "role": "hook",
            "narration": sample,
            "source_scene_id": "part_01_hook",
            "source_role": "hook",
            "source_scene_signature": scenes.expected_source_signature(extra),
            "generation_signature": "c" * 64,
            "scene_prompt_version": scenes.PROMPT_VERSION,
            "is_extra_scene": True,
            "visual_prompt": "A dark house at night.",
            "negative_prompt": "blurry, distorted",
            "camera_prompt": "slow push in",
            "lighting_prompt": "moonlight",
            "voiceover_enabled": True,
            "narration_source": "ai_generated_role_narration",
            "source_text_sha256": scenes.text_sha256(sample),
            "source_word_count": expected,
        }
        self.assertTrue(scenes.scene_is_valid(record, extra, "c" * 64))
        # Reproduce the old Unicode word-boundary counter, which drops the isolated em dash token.
        record["source_word_count"] = len(
            re.findall(r"\b[\w'-]+\b", sample, flags=re.UNICODE)
        )
        self.assertNotEqual(record["source_word_count"], expected)
        self.assertFalse(scenes.scene_is_valid(record, extra, "c" * 64))

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
