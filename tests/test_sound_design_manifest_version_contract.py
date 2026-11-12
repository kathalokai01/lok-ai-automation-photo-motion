from pathlib import Path
import ast
import unittest


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_sound_design.py"
VALIDATOR = ROOT / "scripts" / "validate_sfx_timing.py"


def assigned_string(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
    raise AssertionError(f"Missing string constant: {name}")


class SoundDesignManifestVersionContractTests(unittest.TestCase):
    def test_validator_accepts_exact_generator_manifest_version(self):
        generator_tree = ast.parse(GENERATOR.read_text(encoding="utf-8"))
        validator_tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
        generated_version = assigned_string(generator_tree, "VERSION")
        accepted_version = assigned_string(validator_tree, "SOUND_DESIGN_VERSION")
        self.assertEqual(accepted_version, generated_version)

    def test_validator_version_is_explicitly_versioned(self):
        validator_tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
        validator_version = assigned_string(validator_tree, "VERSION")
        self.assertIn("sfx-validator-v16-", validator_version)


if __name__ == "__main__":
    unittest.main()
