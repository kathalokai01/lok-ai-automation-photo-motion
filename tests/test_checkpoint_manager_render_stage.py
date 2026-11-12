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



def test_depth_map_stage_merges_when_progress_restore_created_destination(tmp_path):
    source = tmp_path / "checkpoint" / "output"
    output = tmp_path / "output"
    source_depth = source / "visuals" / "depth_maps"
    destination_depth = output / "visuals" / "depth_maps"
    source_depth.mkdir(parents=True)
    destination_depth.mkdir(parents=True)

    (source_depth / "depth_progress.json").write_text(
        '{"stage": "depth_maps"}', encoding="utf-8"
    )
    (source_depth / "depth_manifest.json").write_text(
        '{"status": "completed"}', encoding="utf-8"
    )
    (source_depth / "depth_0001.png").write_bytes(b"verified-depth-map")

    # Progress checkpoint merging can create this directory before stage copy.
    (destination_depth / "depth_progress.json").write_text(
        '{"stage": "depth_maps"}', encoding="utf-8"
    )

    copied = checkpoint_manager.copy_stage(source, output, "depth_maps")

    assert copied == 2
    assert (destination_depth / "depth_manifest.json").is_file()
    assert (destination_depth / "depth_0001.png").read_bytes() == b"verified-depth-map"


def test_photo_motion_stage_merges_when_progress_restore_created_destination(tmp_path):
    source = tmp_path / "checkpoint" / "output"
    output = tmp_path / "output"
    source_motion = source / "photo_motion"
    destination_motion = output / "photo_motion"
    source_motion.mkdir(parents=True)
    destination_motion.mkdir(parents=True)

    (source_motion / "photo_motion_manifest.json").write_text(
        '{"status": "completed"}', encoding="utf-8"
    )
    (source_motion / "part_01.mp4").write_bytes(b"verified-photo-motion")

    (destination_motion / "progress.json").write_text(
        '{"stage": "photo_motion"}', encoding="utf-8"
    )

    copied = checkpoint_manager.copy_stage(source, output, "photo_motion")

    assert copied == 2
    assert (destination_motion / "photo_motion_manifest.json").is_file()
    assert (destination_motion / "part_01.mp4").read_bytes() == b"verified-photo-motion"



def test_visual_stage_merges_when_partial_image_directory_exists(tmp_path):
    source = tmp_path / "checkpoint" / "output"
    output = tmp_path / "output"
    source_visuals = source / "visuals"
    destination_visuals = output / "visuals"
    (source_visuals / "images").mkdir(parents=True)
    (destination_visuals / "images").mkdir(parents=True)

    (source_visuals / "images" / "scene_01.png").write_bytes(b"scene-one")
    (source_visuals / "images" / "scene_02.png").write_bytes(b"scene-two")
    (destination_visuals / "images" / "scene_01.png").write_bytes(b"scene-one")

    copied = checkpoint_manager.copy_stage(source, output, "visuals")

    assert copied == 1
    assert (destination_visuals / "images" / "scene_02.png").read_bytes() == b"scene-two"
