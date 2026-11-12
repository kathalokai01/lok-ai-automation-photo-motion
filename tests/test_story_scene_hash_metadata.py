import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_story


class StorySceneHashMetadataTests(unittest.TestCase):
    def build_story(self, policy_path):
        narration = "एक छोटा वाक्य।"
        pending_hook = {
            "scene_id": "part_01_hook",
            "role": "hook",
            "narration": "",
            "voiceover_enabled": False,
            "narration_source": "pending_ai_narration",
        }
        pending_resolution = {
            "scene_id": "part_01_resolution",
            "role": "resolution",
            "narration": "",
            "voiceover_enabled": False,
            "narration_source": "pending_ai_narration",
        }
        plan = {
            "source_fingerprint": "plan-fingerprint",
            "parts": [{
                "part": 1,
                "narration": narration,
                "scenes": [{
                    "scene_id": "part_01_scene_01",
                    "narration": narration,
                    "story_anchor": narration,
                    "visual_prompt": "A quiet village lane at dawn",
                }],
                "hook_scene": pending_hook,
                "suspense_scene": None,
                "resolution_scene": pending_resolution,
            }],
        }
        with patch.object(generate_story, "POLICY_FILE", policy_path):
            return generate_story.make_story(
                title="एक छोटा शीर्षक",
                topic="गाँव",
                mode="full",
                source_text=narration,
                complete_story=narration,
                parts=[narration],
                plan=plan,
                scenes_range=(1, 1),
                model="test-model",
                proofreading={},
                policy={},
                selection={
                    "minimum_parts": 1,
                    "maximum_parts": 1,
                    "final_part_merged": False,
                },
                source_fingerprint="source-fingerprint",
            )

    def test_make_story_writes_scene_hash_and_word_count(self):
        with tempfile.TemporaryDirectory() as directory:
            story = self.build_story(Path(directory) / "policy.json")
        scene = story["parts"][0]["scenes"][0]
        expected = hashlib.sha256(scene["narration"].encode("utf-8")).hexdigest()
        self.assertEqual(scene["source_text_sha256"], expected)
        self.assertEqual(scene["source_word_count"], 3)

    def test_story_validator_rejects_changed_scene_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            story = self.build_story(Path(directory) / "policy.json")
        story["parts"][0]["scenes"][0]["source_text_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "Scene source text hash mismatch"):
            generate_story.validate_story(story, 1, (1, 1), "full")


if __name__ == "__main__":
    unittest.main()
