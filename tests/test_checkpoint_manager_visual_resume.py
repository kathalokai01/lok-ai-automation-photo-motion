from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts import checkpoint_manager


def make_visual_checkpoint(root: Path, count: int = 12, corrupt_index: int | None = None):
    source = root / "saved"
    visuals = source / "visuals"
    images = visuals / "images"
    images.mkdir(parents=True)
    source_fp = "a" * 64
    config_sig = "b" * 64
    jobs, rows, hashes = [], [], {}
    for index in range(1, count + 1):
        scene_id = f"part_01_scene_{index:02d}"
        filename = f"0001_scene_{index:02d}.png"
        image_path = images / filename
        Image.new("RGB", (720, 1280), (45 + index, 90, 130)).save(image_path)
        checksum = hashlib.sha256(image_path.read_bytes()).hexdigest()
        hashes[filename] = checksum
        relative = f"output/visuals/images/{filename}"
        prompt_sig = hashlib.sha256(f"prompt-{index}".encode()).hexdigest()
        prompt_hash = hashlib.sha256(f"prompt-hash-{index}".encode()).hexdigest()
        scene_sig = hashlib.sha256(f"scene-{index}".encode()).hexdigest()
        job = {
            "scene_id": scene_id, "part": 1, "global_scene": index,
            "role": "main", "image_path": relative, "image_size": "720x1280",
            "prompt_sha256": prompt_hash, "prompt_signature": prompt_sig,
            "source_scene_signature": scene_sig, "config_signature": config_sig,
            "negative_prompt": "bad anatomy",
        }
        row = {
            "scene_id": scene_id, "part": 1, "global_scene": index,
            "role": "main", "image_path": relative,
            "image_sha256": ("0" * 64 if index == corrupt_index else checksum),
            "prompt_sha256": prompt_hash, "prompt_signature": prompt_sig,
            "source_scene_signature": scene_sig, "job_signature": "f" * 64,
            "config_signature": config_sig, "source_fingerprint": source_fp,
            "image_size": "720x1280", "width": 720, "height": 1280,
            "status": "generated",
        }
        jobs.append(job)
        rows.append(row)
    (visuals / "visual_jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    jobs_manifest = {
        "status": "completed", "source_fingerprint": source_fp,
        "config_signature": config_sig, "format": "short",
    }
    (visuals / "visual_jobs_manifest.json").write_text(json.dumps(jobs_manifest), encoding="utf-8")
    for name in ("image_manifest.json", "image_generation_manifest.json"):
        manifest = {
            "status": "completed", "source_fingerprint": source_fp,
            "config_signature": config_sig, "format": "short",
            "image_size": "720x1280", "scenes": [dict(row) for row in rows],
        }
        (visuals / name).write_text(json.dumps(manifest), encoding="utf-8")
    return source, hashes, source_fp, config_sig


class CheckpointManagerVisualResumeTests(unittest.TestCase):
    def test_restores_twelve_verified_images_and_rebuilds_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _, _ = make_visual_checkpoint(root, count=12)
            output = root / "output"
            self.assertEqual(
                checkpoint_manager.restore_partial_visual_images(source, output), 12
            )
            progress = json.loads(
                (output / "checkpoints" / "visual_jobs_progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(progress["completed_jobs"], 12)
            self.assertEqual(progress["remaining_jobs"], 0)
            self.assertEqual(len(progress["scene_progress"]), 12)

    def test_hash_mismatch_rejects_only_unverified_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _, _ = make_visual_checkpoint(root, count=12, corrupt_index=5)
            output = root / "output"
            self.assertEqual(
                checkpoint_manager.restore_partial_visual_images(source, output), 11
            )
            progress = json.loads(
                (output / "checkpoints" / "visual_jobs_progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(progress["completed_jobs"], 11)
            self.assertEqual(progress["remaining_jobs"], 1)

    def test_merged_progress_json_uses_real_newline_and_parses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "saved"
            checkpoint_dir = source / "checkpoints"
            checkpoint_dir.mkdir(parents=True)
            payload = {
                "source_fingerprint": "a" * 64,
                "config_signature": "b" * 64,
                "scene_progress": [{"scene_id": "scene-1", "status": "completed"}],
            }
            (checkpoint_dir / "visual_jobs_progress.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            output = root / "output"
            checkpoint_manager.merge_progress_checkpoints(source, output)
            target = output / "checkpoints" / "visual_jobs_progress.json"
            raw = target.read_text(encoding="utf-8")
            self.assertTrue(raw.endswith("\n"))
            self.assertFalse(raw.endswith("\\n"))
            self.assertFalse(raw.endswith("\n\n"))
            self.assertEqual(json.loads(raw)["scene_progress"][0]["scene_id"], "scene-1")
            # Restoring must canonicalize the copied destination without
            # mutating the original checkpoint artifact.
            self.assertFalse(
                (checkpoint_dir / "visual_jobs_progress.json")
                .read_text(encoding="utf-8").endswith("\n")
            )

    def test_corrupt_restored_json_is_quarantined_without_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()
            corrupt = output / "story.json"
            original = '{"story": true}\\n{"extra": true}'
            corrupt.write_text(original, encoding="utf-8")
            count = checkpoint_manager.quarantine_invalid_json_files(output)
            self.assertEqual(count, 1)
            self.assertFalse(corrupt.exists())
            preserved = list(output.glob("story.json.invalid-*"))
            self.assertEqual(len(preserved), 1)
            self.assertEqual(preserved[0].read_text(encoding="utf-8"), original)

    def test_valid_checkpoint_replaces_corrupt_existing_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "saved" / "checkpoints"
            output_dir = root / "output" / "checkpoints"
            source_dir.mkdir(parents=True)
            output_dir.mkdir(parents=True)
            payload = {
                "source_fingerprint": "a" * 64,
                "config_signature": "b" * 64,
                "scene_progress": [{"scene_id": "restored-scene", "status": "completed"}],
            }
            relative = "visual_jobs_progress.json"
            (source_dir / relative).write_text(json.dumps(payload), encoding="utf-8")
            (output_dir / relative).write_text(
                '{"broken": true}\\n{"second": true}', encoding="utf-8"
            )
            checkpoint_manager.merge_progress_checkpoints(
                root / "saved", root / "output"
            )
            restored = json.loads((output_dir / relative).read_text(encoding="utf-8"))
            self.assertEqual(restored["scene_progress"][0]["scene_id"], "restored-scene")

    def test_partial_audio_asset_is_copied_only_after_sha256_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "saved"
            sound_dir = source / "sound_design"
            sound_dir.mkdir(parents=True)
            asset = sound_dir / "vehicle_sfx.wav"
            asset.write_bytes(b"verified-audio-artifact")
            checksum = hashlib.sha256(asset.read_bytes()).hexdigest()
            checkpoint_dir = source / "checkpoints"
            checkpoint_dir.mkdir()
            checkpoint = {
                "source_fingerprint": "a" * 64,
                "tracks": [{
                    "path": "output/sound_design/vehicle_sfx.wav",
                    "sha256": checksum,
                }],
            }
            (checkpoint_dir / "sound_design_progress.json").write_text(
                json.dumps(checkpoint), encoding="utf-8"
            )
            output = root / "output"
            checkpoint_manager.merge_progress_checkpoints(source, output)
            restored = output / "sound_design" / "vehicle_sfx.wav"
            self.assertTrue(restored.is_file())
            self.assertEqual(hashlib.sha256(restored.read_bytes()).hexdigest(), checksum)

    def test_run_enumerator_includes_success_failure_and_cancelled(self):
        payload = "11\n12\n13\n"
        captured = {}
        def fake_run(command, **kwargs):
            captured["command"] = command
            return type("Result", (), {
                "returncode": 0, "stdout": payload, "stderr": ""
            })()
        with patch.object(checkpoint_manager.subprocess, "run", side_effect=fake_run):
            self.assertEqual(
                checkpoint_manager.list_runs("owner/repo", "workflow.yml", "main", 0),
                [11, 12, 13],
            )
        self.assertIn("--paginate", captured["command"])
        self.assertIn("--jq", captured["command"])
        self.assertNotIn("--slurp", captured["command"])
        self.assertIn("status=completed", captured["command"][-1])


if __name__ == "__main__":
    unittest.main()
