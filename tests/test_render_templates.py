import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import render_templates
class TemplateTests(unittest.TestCase):
 def test_templates_are_known_and_isolated(self):
  self.assertIn("documentary",render_templates.template_names())
  item=render_templates.get_template(); item["transition"]="bad"
  self.assertNotEqual(render_templates.get_template()["transition"],"bad")
