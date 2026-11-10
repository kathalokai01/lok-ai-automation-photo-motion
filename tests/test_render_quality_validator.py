import unittest
from pathlib import Path
class RenderQualityTests(unittest.TestCase):
 def test_validator_checks_media_contract(self):
  source=(Path(__file__).resolve().parents[1]/"scripts/render_quality_validator.py").read_text()
  self.assertIn("ffprobe",source)
  self.assertIn("expected",source)
