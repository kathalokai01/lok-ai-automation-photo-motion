import hashlib
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render_video


def test_renderer_signature_version_matches_validator():
    validator = (ROOT / "scripts" / "validate_sfx_timing.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION = "([^"]+)"$', validator, re.MULTILINE)
    assert match is not None
    assert render_video.SFX_VALIDATOR_VERSION == match.group(1)


def test_renderer_builds_validator_signature_from_all_three_source_hashes():
    audio_hash = "a" * 64
    sound_hash = "b" * 64
    cue_hash = "c" * 64
    expected = hashlib.sha256(
        (audio_hash + sound_hash + cue_hash + render_video.SFX_VALIDATOR_VERSION).encode("utf-8")
    ).hexdigest()

    assert render_video.sfx_validation_signature(
        audio_hash, sound_hash, cue_hash
    ) == expected
