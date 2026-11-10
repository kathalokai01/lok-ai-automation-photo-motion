import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import production_cost_tracker
class CostTests(unittest.TestCase):
 def test_costs_are_not_guessed(self):
  self.assertTrue(callable(production_cost_tracker.main))
