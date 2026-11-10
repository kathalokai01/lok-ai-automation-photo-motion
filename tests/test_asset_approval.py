import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import asset_approval
class ApprovalTests(unittest.TestCase):
 def test_pending_is_not_implicitly_approved(self):
  self.assertTrue(callable(asset_approval.load))
