import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import production_events
class EventTests(unittest.TestCase):
 def test_event_roundtrip(self):
  with tempfile.TemporaryDirectory() as d:
   with patch.object(production_events,"EVENTS",Path(d)/"events.jsonl"),patch.object(production_events,"STATE",Path(d)/"state.json"):
    production_events.record_event("render","completed")
    self.assertEqual(production_events.stage_summary()["completed"],1)
