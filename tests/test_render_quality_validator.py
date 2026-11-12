import unittest
from pathlib import Path


class RenderQualityTests(unittest.TestCase):
    def setUp(self):
        self.source = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "render_quality_validator.py"
        ).read_text(encoding="utf-8")

    def test_validator_checks_media_contract(self):
        self.assertIn("ffprobe", self.source)
        self.assertIn("expected", self.source)
        self.assertIn("render_manifest.json", self.source)

    def test_direct_execution_adds_repository_root_before_package_import(self):
        root_setup = 'sys.path.insert(0, str(ROOT))'
        package_import = 'from scripts.input_config import'
        self.assertIn("import sys", self.source)
        self.assertIn(root_setup, self.source)
        self.assertLess(
            self.source.index(root_setup),
            self.source.index(package_import),
        )

    def test_validator_rejects_wrong_format_dimensions_and_fps(self):
        self.assertIn("dimensions", self.source)
        self.assertIn("FPS mismatch", self.source)
        self.assertIn("Wrong {mode} dimensions", self.source)

    def test_validator_requires_audio_and_video_streams(self):
        self.assertIn("if not video or not audio", self.source)
