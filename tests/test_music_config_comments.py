import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import generate_flow_music


class MusicInlineCommentTests(unittest.TestCase):
    def test_boolean_setting_ignores_unquoted_inline_comment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "topic.txt"
            path.write_text(
                'MUSIC_ALLOW_PAID = true # approved paid provider setting\n'
                'MUSIC_STYLE = "cinematic # score" # trailing note\n',
                encoding="utf-8",
            )
            with patch.object(generate_flow_music, "INPUT_FILE", path):
                settings = generate_flow_music.read_settings()
        self.assertEqual(settings["MUSIC_ALLOW_PAID"], "true")
        self.assertEqual(settings["MUSIC_STYLE"], "cinematic # score")

    def test_paid_boolean_is_valid_after_comment_removal(self):
        self.assertTrue(generate_flow_music.truthy("true", False))


if __name__ == "__main__":
    unittest.main()
