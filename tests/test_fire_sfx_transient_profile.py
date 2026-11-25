from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_sound_design


def test_fire_sfx_uses_decaying_crackle_not_single_sample_impulses():
    samples = list(generate_sound_design.sfx_samples("fire", duration=2.0, seed=12345))
    assert samples
    peak = max(abs(value) for value in samples)
    rms = (sum(value * value for value in samples) / len(samples)) ** 0.5

    # The old isolated one-sample crack peaks drove the look-ahead limiter
    # and suppressed the mean level of the entire eight-second fire asset.
    assert peak < 0.75
    assert rms > 0.05


def test_all_sfx_gains_are_restrained_below_narration_priority():
    assert max(generate_sound_design.SFX_GAINS.values()) <= 0.38
    renderer = (ROOT / "scripts" / "render_video.py").read_text(encoding="utf-8")
    assert "SFX_GAIN_MULTIPLIER = 0.55" in renderer
    assert "mode=upward[sfx_gated]" in renderer


def test_fire_normalization_profile_invalidates_only_old_fire_asset():
    source = (ROOT / "scripts" / "generate_sound_design.py").read_text(encoding="utf-8")
    assert '"normalization_profile"] = "fire-peak-safe-v4"' in source
    assert 'crack_envelope = 0.0' in source
    assert 'value = 0.24 * noise_state + crack_envelope * noise * 0.18' in source
