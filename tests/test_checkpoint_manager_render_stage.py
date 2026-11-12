import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import checkpoint_manager


def write_render_manifest(output_root, video_path, digest=None):
    manifest_dir = output_root / "scene_renders"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    row = {"scene_id": "part_01_hook", "path": video_path}
    if digest is not None:
        row["sha256"] = digest
    manifest = {
        "status": "completed",
        "all_completed_scenes": [row],
        "parts": [],
    }
    (manifest_dir / "render_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def test_render_checkpoint_is_rejected_when_referenced_part_video_is_missing(tmp_path):
    write_render_manifest(
        tmp_path,
        "output/parts/part_01.mp4",
        "a" * 64,
    )

    assert not checkpoint_manager.validate_stage(
        "scene_renders",
        tmp_path,
        {},
    )


def test_render_checkpoint_is_accepted_only_when_asset_and_sha256_match(tmp_path):
    video = tmp_path / "parts" / "part_01.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"verified render output")
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    write_render_manifest(
        tmp_path,
        "output/parts/part_01.mp4",
        digest,
    )

    assert checkpoint_manager.validate_stage(
        "scene_renders",
        tmp_path,
        {},
    )


def test_render_checkpoint_rejects_asset_hash_mismatch(tmp_path):
    video = tmp_path / "parts" / "part_01.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"render output with wrong hash")
    write_render_manifest(
        tmp_path,
        "output/parts/part_01.mp4",
        "0" * 64,
    )

    assert not checkpoint_manager.validate_stage(
        "scene_renders",
        tmp_path,
        {},
    )


def test_render_checkpoint_maps_absolute_runner_path_to_checkpoint_output(tmp_path):
    video = tmp_path / "parts" / "part_01.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"absolute path render")
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    write_render_manifest(
        tmp_path,
        "/home/runner/work/old-repo/old-repo/output/parts/part_01.mp4",
        digest,
    )

    assert checkpoint_manager.validate_stage(
        "scene_renders",
        tmp_path,
        {},
    )
