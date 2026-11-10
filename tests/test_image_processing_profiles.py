import tempfile,unittest,sys
from pathlib import Path
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import image_processing_profiles as profiles
class ProfileTests(unittest.TestCase):
 def test_profile_output(self):
  with tempfile.TemporaryDirectory() as d:
   src=Path(d)/"in.png"; dst=Path(d)/"out.jpg"
   Image.new("RGB",(32,32),(80,90,100)).save(src)
   profiles.apply_profile(src,dst,"natural")
   self.assertTrue(dst.is_file())
