import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import package_output


class PackageNarrationManifestTests(unittest.TestCase):
    def make_rows(self, role="hook"):
        text = "एक भव्य उद्घाटन और छिपा हुआ बग।"
        source = (
            "ai_generated_role_narration"
            if role in {"hook", "suspense", "resolution"}
            else "locked_part_text"
        )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return (
            {
                "scene_id": "part_01_hook",
                "role": role,
                "voiceover_enabled": True,
                "narration": None,
                "narration_source": source,
                "source_text_sha256": digest,
            },
            {
                "scene_id": "part_01_hook",
                "role": role,
                "voiceover_enabled": True,
                "narration": text,
                "text": text,
                "narration_source": source,
            },
            {
                "scene_id": "part_01_hook",
                "role": role,
                "status": "completed",
                "voiceover_enabled": True,
                "text": text,
                "text_sha256": digest,
                "narration_source": source,
            },
            text,
        )

    def test_hook_uses_narration_json_when_photo_manifest_text_is_null(self):
        photo, narration, audio, expected = self.make_rows("hook")
        result = package_output.resolve_scene_narration(
            photo, narration, audio, "part_01_hook", "hook"
        )
        self.assertEqual(result, expected)

    def test_resolution_uses_narration_json_when_photo_manifest_text_is_null(self):
        photo, narration, audio, expected = self.make_rows("resolution")
        result = package_output.resolve_scene_narration(
            photo, narration, audio, "part_01_resolution", "resolution"
        )
        self.assertEqual(result, expected)

    def test_missing_narration_record_is_rejected(self):
        photo, _, audio, _ = self.make_rows("hook")
        with self.assertRaisesRegex(RuntimeError, "Narration metadata record is missing"):
            package_output.resolve_scene_narration(
                photo, None, audio, "part_01_hook", "hook"
            )

    def test_tts_text_mismatch_is_rejected(self):
        photo, narration, audio, _ = self.make_rows("hook")
        audio["text"] = "अलग TTS पाठ।"
        with self.assertRaisesRegex(RuntimeError, "differs from TTS source text"):
            package_output.resolve_scene_narration(
                photo, narration, audio, "part_01_hook", "hook"
            )

    def test_narration_scene_id_mismatch_is_rejected(self):
        photo, narration, audio, _ = self.make_rows("hook")
        narration["scene_id"] = "part_01_resolution"
        with self.assertRaisesRegex(RuntimeError, "Narration metadata scene ID mismatch"):
            package_output.resolve_scene_narration(
                photo, narration, audio, "part_01_hook", "hook"
            )

    def test_narration_source_role_mismatch_is_rejected(self):
        photo, narration, audio, _ = self.make_rows("hook")
        photo["narration_source"] = "locked_part_text"
        with self.assertRaisesRegex(RuntimeError, "Narration source mismatch"):
            package_output.resolve_scene_narration(
                photo, narration, audio, "part_01_hook", "hook"
            )


if __name__ == "__main__":
    unittest.main()
