import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import checkpoint_manager


def test_failed_run_partial_tts_chunks_are_restored_and_verified(tmp_path):
    source = tmp_path / "checkpoint" / "output"
    output = tmp_path / "output"
    audio = source / "narration" / "audio" / "scene-chunk-001.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"verified partial tts audio")
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()

    checkpoint_dir = source / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    checkpoint = {
        "schema_version": 1,
        "input_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "completed_chunks": [
            {
                "job_id": "1:part_01_scene_01:1:abc123",
                "output_path": "output/narration/audio/scene-chunk-001.mp3",
                "sha256": digest,
                "duration": 2.5,
                "provider": "edge_tts",
            }
        ],
        "completed_count": 1,
    }
    (checkpoint_dir / "tts_progress.json").write_text(
        json.dumps(checkpoint), encoding="utf-8"
    )

    restored = checkpoint_manager.restore_partial_tts_chunks(source, output)

    restored_audio = output / "narration" / "audio" / "scene-chunk-001.mp3"
    assert restored == 1
    assert restored_audio.read_bytes() == audio.read_bytes()

    copied_progress = checkpoint_manager.merge_progress_checkpoints(source, output)
    assert copied_progress == 1
    assert (output / "checkpoints" / "tts_progress.json").is_file()


def test_partial_tts_restore_rejects_audio_hash_mismatch(tmp_path):
    source = tmp_path / "checkpoint" / "output"
    output = tmp_path / "output"
    audio = source / "narration" / "audio" / "scene-chunk-001.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio with wrong checkpoint hash")

    checkpoint_dir = source / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    checkpoint = {
        "schema_version": 1,
        "input_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "completed_chunks": [
            {
                "job_id": "1:part_01_scene_01:1:abc123",
                "output_path": "output/narration/audio/scene-chunk-001.mp3",
                "sha256": "0" * 64,
                "duration": 2.5,
                "provider": "edge_tts",
            }
        ],
        "completed_count": 1,
    }
    (checkpoint_dir / "tts_progress.json").write_text(
        json.dumps(checkpoint), encoding="utf-8"
    )

    assert checkpoint_manager.restore_partial_tts_chunks(source, output) == 0
    assert not (output / "narration" / "audio" / "scene-chunk-001.mp3").exists()
