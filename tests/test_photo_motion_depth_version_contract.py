import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def assigned_string(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
    raise AssertionError(f"{name} string assignment not found in {path}")


class PhotoMotionDepthVersionContractTests(unittest.TestCase):
    def test_renderer_accepts_current_depth_generator_version(self):
        depth_version = assigned_string(
            ROOT / "scripts" / "generate_depth_maps.py", "VERSION"
        )
        renderer = (ROOT / "scripts" / "photo_motion.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            f'document.get("version") != "{depth_version}"',
            renderer,
            "Photo Motion must validate the exact depth generator manifest version.",
        )


if __name__ == "__main__":
    unittest.main()
