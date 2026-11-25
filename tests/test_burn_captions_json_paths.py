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
    assert 'VERSION = "burn-captions-v67-reject-unconverted-hindi-in-hinglish"' in source
    assert "default=json_path_default" in source
    assert "def json_path_default(value):" in source
    assert "isinstance(value, os.PathLike)" in source


def test_global_srt_cues_are_quantized_and_non_overlapping(tmp_path):
    cues = [
        {"start": 0.0004, "end": 1.5014, "text": "पहला वाक्य"},
        {"start": 1.5006, "end": 2.2504, "text": "दूसरा वाक्य"},
        {"start": 2.2502, "end": 3.0004, "text": "तीसरा वाक्य"},
    ]

    normalized = burn_captions.normalize_srt_cues(cues)
    assert len(normalized) == 3

    previous_end = -1.0
    for cue in normalized:
        assert cue["start"] >= previous_end
        assert cue["end"] > cue["start"]
        assert round(cue["start"] * 1000) == cue["start"] * 1000
        assert round(cue["end"] * 1000) == cue["end"] * 1000
        previous_end = cue["end"]

    target = tmp_path / "final.srt"
    burn_captions.write_srt(target, cues)
    content = target.read_text(encoding="utf-8")
    assert "पहला वाक्य" in content
    assert "दूसरा वाक्य" in content
    assert "तीसरा वाक्य" in content
    assert content.count(" --> ") == 3
