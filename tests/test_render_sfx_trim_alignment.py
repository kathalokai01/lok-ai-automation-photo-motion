from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render_video


def test_renderer_uses_validator_shift_and_trim_for_hook_cue():
    cue = {
        "start_seconds": 1.736,
        "duration_seconds": 8.0,
        "renderer_start_seconds": 0.0,
        "renderer_effective_duration_seconds": 7.889,
        "renderer_truncated_seconds": 0.111,
        "renderer_end_seconds": 7.889,
    }

    assert render_video.validated_sfx_timing(cue, 7.889, "part_01_hook") == (0.0, 7.889)


def test_renderer_uses_trimmed_duration_for_short_scene_cue():
    cue = {
        "start_seconds": 0.0,
        "duration_seconds": 8.0,
        "renderer_start_seconds": 0.0,
        "renderer_effective_duration_seconds": 2.978,
        "renderer_truncated_seconds": 5.022,
        "renderer_end_seconds": 2.978,
    }

    assert render_video.validated_sfx_timing(cue, 2.978, "part_01_scene_02") == (0.0, 2.978)


def test_renderer_rejects_unvalidated_or_inconsistent_trim_metadata():
    cue = {
        "start_seconds": 1.0,
        "duration_seconds": 8.0,
        "renderer_start_seconds": 0.0,
        "renderer_effective_duration_seconds": 7.0,
        "renderer_truncated_seconds": 0.5,
        "renderer_end_seconds": 7.0,
    }

    try:
        render_video.validated_sfx_timing(cue, 7.0, "part_01_hook")
    except RuntimeError as exc:
        assert "trim metadata is inconsistent" in str(exc)
    else:
        raise AssertionError("Inconsistent validator trim metadata was accepted")


def test_renderer_version_records_validated_sfx_timeline_contract():
    source = (ROOT / "scripts" / "render_video.py").read_text(encoding="utf-8")
    assert 'VERSION = "render-video-v70-consume-validated-sfx-trim-timeline"' in source
    assert "def validated_sfx_timing(" in source
