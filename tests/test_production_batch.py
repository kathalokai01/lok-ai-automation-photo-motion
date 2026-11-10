import tempfile,unittest,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import production_batch
class BatchTests(unittest.TestCase):
 def test_queue_does_not_dispatch(self):
  with tempfile.TemporaryDirectory() as d:
   with patch.object(production_batch,"QUEUE",Path(d)/"queue.json"):
    job=production_batch.add("A test story","short")
    self.assertEqual(job["status"],"queued")
