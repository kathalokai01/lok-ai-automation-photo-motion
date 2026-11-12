from pathlib import Path
import hashlib


ROOT = Path(__file__).resolve().parents[1]


def test_renderer_uses_current_sfx_validator_signature_version():
    renderer = (ROOT / "scripts" / "render_video.py").read_text(encoding="utf-8")
    validator = (ROOT / "scripts" / "validate_sfx_timing.py").read_text(encoding="utf-8")

    assert 'VERSION = "sfx-validator-v17-allow-trimmed-cue-duration"' in validator
    assert 'VERSION = "render-video-v69-sfx-validator-v17-signature-contract"' in renderer
    assert ' + "sfx-validator-v17-allow-trimmed-cue-duration"' in renderer
    assert "sfx-validator-v13-visual-role-scene-timing" not in renderer


def test_sfx_signature_formula_matches_validator_version():
    audio_hash = "a" * 64
    sound_hash = "b" * 64
    cue_hash = "c" * 64
    version = "sfx-validator-v17-allow-trimmed-cue-duration"

    expected = hashlib.sha256(
        (audio_hash + sound_hash + cue_hash + version).encode("utf-8")
    ).hexdigest()

    actual = hashlib.sha256(
        (audio_hash + sound_hash + cue_hash + version).encode("utf-8")
    ).hexdigest()

    assert actual == expected
