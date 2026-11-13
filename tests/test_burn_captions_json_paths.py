from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import burn_captions


def test_write_json_serializes_nested_path_objects_as_strings(tmp_path):
    target = tmp_path / "manifest.json"
    nested_path = tmp_path / "parts" / "part_01.mp4"

    burn_captions.write_json(
        target,
        {"part": {"path": nested_path, "items": [nested_path]}},
    )

    saved = json.loads(target.read_text(encoding="utf-8"))
    expected = str(nested_path)
    assert saved["part"]["path"] == expected
    assert saved["part"]["items"] == [expected]


def test_write_json_keeps_rejecting_unhandled_object_types(tmp_path):
    target = tmp_path / "manifest.json"

    try:
        burn_captions.write_json(target, {"invalid": object()})
    except TypeError as exc:
        assert "not JSON serializable" in str(exc)
    else:
        raise AssertionError("Unsupported object was silently serialized")

    assert not target.exists()
    assert not target.with_name(target.name + ".tmp").exists()


def test_caption_burn_path_serialization_contract_is_present():
    source = (ROOT / "scripts" / "burn_captions.py").read_text(encoding="utf-8")
    assert 'VERSION = "burn-captions-v66-low-position-vivid-colour-thin-outline"' in source
    assert "default=json_path_default" in source
    assert "def json_path_default(value):" in source
    assert "isinstance(value, os.PathLike)" in source
