import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import production_events
class DashboardTests(unittest.TestCase):
 def test_stage_summary_contract(self):
  self.assertTrue(callable(production_events.stage_summary))
  self.assertIn("completed",production_events.stage_summary())
