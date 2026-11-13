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


def test_renderer_exposes_current_sfx_timeline_contract():
    source = (ROOT / "scripts" / "render_video.py").read_text(encoding="utf-8")
    assert f'VERSION = "{render_video.VERSION}"' in source
    assert "def validated_sfx_timing(" in source
    assert 'SFX_VALIDATOR_VERSION = "sfx-validator-v17-allow-trimmed-cue-duration"' in source


def test_renderer_emits_part_videos_only_without_combined_intermediate():
    source = (ROOT / "scripts" / "render_video.py").read_text(encoding="utf-8")
    assert '"part_outputs_only": True' in source
    assert "PRE_CAPTION_VIDEO" not in source
    assert "pre_caption_video" not in source
    assert "ordered_part_paths" not in source
    assert f'VERSION = "{render_video.VERSION}"' in source


def test_audio_filter_splits_narration_for_sidechain_and_final_mix():
    old = render_video.MUSIC_DUCKING_ENABLED
    render_video.MUSIC_DUCKING_ENABLED = True
    try:
        _, filters = render_video.build_audio_filter(
            {"video": "scene.mp4", "duration": 4.0, "scene_id": "part_01_hook"},
            {"audio": "voice.wav", "duration": 4.0},
            {"music": {"path": "music.wav", "duration": 30.0}, "ambient": None, "sfx": {}},
            0.0, 720, 1280, 24,
        )
    finally:
        render_video.MUSIC_DUCKING_ENABLED = old

    graph = ";".join(filters)
    assert "[narr]asplit=2[narr_mix][narr_sc]" in graph
    assert "[bed][narr_sc]sidechaincompress=" in graph
    assert "[narr_mix]" in graph
    assert graph.endswith("[aout]")


def test_audio_filter_keeps_single_narration_branch_when_ducking_disabled():
    old = render_video.MUSIC_DUCKING_ENABLED
    render_video.MUSIC_DUCKING_ENABLED = False
    try:
        _, filters = render_video.build_audio_filter(
            {"video": "scene.mp4", "duration": 4.0, "scene_id": "part_01_hook"},
            {"audio": "voice.wav", "duration": 4.0},
            {"music": {"path": "music.wav", "duration": 30.0}, "ambient": None, "sfx": {}},
            0.0, 720, 1280, 24,
        )
    finally:
        render_video.MUSIC_DUCKING_ENABLED = old

    graph = ";".join(filters)
    assert "asplit=" not in graph
    assert "[narr]" in graph
    assert "[bed]" in graph
