import hashlib
import json
from pathlib import Path

from scripts import checkpoint_manager


def build_sound_checkpoint(root: Path, *, stale_hash: bool = False, omit_hash: bool = False):
    asset = root / "sfx" / "weather.wav"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"current weather effect")
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    declared = ("0" * 64) if stale_hash else digest

    rows = [{"path": "output/sfx/weather.wav", "sha256": declared}]
    cue_rows = [{"file": "output/sfx/weather.wav", "sha256": declared}]
    if omit_hash:
        rows[0].pop("sha256")
        cue_rows[0].pop("sha256")

    design_dir = root / "sound_design"
    design_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        "sound_design_manifest.json": {
            "status": "completed",
            "mix_records": rows,
        },
        "sfx_cues.json": {
            "status": "completed",
            "cues": cue_rows,
        },
        "sfx_timing_validation.json": {
            "status": "completed",
            "cues": cue_rows,
        },
    }
    for name, document in documents.items():
        (design_dir / name).write_text(
            json.dumps(document), encoding="utf-8"
        )


def test_sound_design_checkpoint_rejects_stale_sfx_hash(tmp_path):
    build_sound_checkpoint(tmp_path, stale_hash=True)

    assert not checkpoint_manager.validate_stage(
        "sound_design", tmp_path, {}
    )


def test_sound_design_checkpoint_accepts_only_fully_hash_verified_stage(tmp_path):
    build_sound_checkpoint(tmp_path)

    assert checkpoint_manager.validate_stage(
        "sound_design", tmp_path, {}
    )


def test_sound_design_checkpoint_rejects_missing_asset_hash(tmp_path):
    build_sound_checkpoint(tmp_path, omit_hash=True)

    assert not checkpoint_manager.validate_stage(
        "sound_design", tmp_path, {}
    )
