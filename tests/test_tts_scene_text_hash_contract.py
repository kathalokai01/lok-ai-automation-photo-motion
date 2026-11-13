import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_tts


class TTSSceneTextHashContractTests(unittest.TestCase):
    def test_hash_metadata_matches_normalized_text(self):
        text = "  पहला वाक्य।\n\nदूसरा   वाक्य। "
        normalized = "पहला वाक्य। दूसरा वाक्य।"
        expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        result = generate_tts.scene_text_hash_metadata({
            "text": text,
            "text_sha256": expected,
        })
        self.assertEqual(result["text_sha256"], expected)
        self.assertEqual(result["source_text_sha256"], expected)

    def test_hash_metadata_rejects_changed_declared_hash(self):
        with self.assertRaises(RuntimeError):
            generate_tts.scene_text_hash_metadata({
                "text": "कहानी का वाक्य।",
                "text_sha256": "0" * 64,
            })

    def test_hash_metadata_rejects_empty_text(self):
        with self.assertRaises(RuntimeError):
            generate_tts.scene_text_hash_metadata({"text": "   "})

    def test_both_reuse_and_generation_paths_write_hash_metadata(self):
        source = (ROOT / "scripts/generate_tts.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("**scene_text_hash_metadata(job)"), 2)
        self.assertIn("VERSION = 23", source)

    def test_renderer_uses_same_whitespace_normalization(self):
        source = (ROOT / "scripts/render_video.py").read_text(encoding="utf-8")
        self.assertIn(
            'normalized_text = re.sub(r"\\s+", " ", text_value).strip()',
            source,
        )
        self.assertIn('row.get("text_sha256") != text_hash', source)


if __name__ == "__main__":
    unittest.main()
