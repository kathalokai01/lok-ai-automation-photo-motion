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

    def test_fire_sfx_has_dedicated_peak_safe_profile(self):
        generator_tree = ast.parse(GENERATOR.read_text(encoding="utf-8"))
        source = GENERATOR.read_text(encoding="utf-8")
        self.assertIn('if kind == "fire":', source)
        self.assertIn('"fire-peak-safe-v3"', source)
        self.assertIn('signature_data["normalization_profile"]', source)
        self.assertIn('loudnorm=I=-16:TP=-6.0:LRA=8', source)
        self.assertNotIn('loudnorm=I=-16:TP=-3.0:LRA=8', source)
        self.assertNotIn('"legacy-sfx-profile-v1"', source)
        self.assertIn('alimiter=limit=0.45:attack=5:release=100:level=disabled', source)
        self.assertIn('"sfx_sample_peak_ceiling_dbfs": -0.1', source)
        self.assertIn('"fire_sfx_sample_peak_ceiling_dbfs": -2.5', source)
        self.assertIn('"fire_sfx_limiter_linear_ceiling": 0.45', source)
        self.assertIn('Generated fire SFX failed its dedicated peak ceiling', source)

    def test_validator_version_is_explicitly_versioned(self):
        validator_tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
        validator_version = assigned_string(validator_tree, "VERSION")
        self.assertIn("sfx-validator-v17-", validator_version)


if __name__ == "__main__":
    unittest.main()
