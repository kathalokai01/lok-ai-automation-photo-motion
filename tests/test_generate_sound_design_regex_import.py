from pathlib import Path
import ast
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "generate_sound_design.py"


class SoundDesignRegexImportTests(unittest.TestCase):
    def test_regex_module_is_imported(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertIn("re", imported)

    def test_regex_operations_used_by_audio_validation_are_available(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
        }
        self.assertIn("fullmatch", calls)
        self.assertIn("sub", calls)


if __name__ == "__main__":
    unittest.main()
