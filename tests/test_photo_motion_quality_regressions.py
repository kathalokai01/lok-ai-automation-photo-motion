from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts import checkpoint_manager
from scripts import generate_visuals
from scripts import photo_motion
from scripts import prepare_captions
from scripts import generate_sound_design
from scripts import visual_quality_validator
from scripts import production_events
from scripts.story_length_policy import verify_final_merge_evidence


class CheckpointRestoreTests(unittest.TestCase):
    def _candidate(self, root: Path, corrupt_hash: bool = False, count: int = 12):
        source = root / "saved"
        visuals = source / "visuals"
        images = visuals / "images"
        images.mkdir(parents=True)
        source_fp = "a" * 64
        config_sig = "b" * 64
        jobs = []
        rows = []
        hashes = {}
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
                "image_sha256": ("0" * 64 if corrupt_hash else checksum),
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
    def test_verified_partial_image_rebuilds_generator_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, hashes, source_fp, config_sig = self._candidate(root, count=12)
            output = root / "output"
            restored = checkpoint_manager.restore_partial_visual_images(source, output)
            self.assertEqual(restored, 12)
            progress_path = output / "checkpoints" / "visual_jobs_progress.json"
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual(progress["completed_jobs"], 12)
            self.assertEqual(progress["remaining_jobs"], 0)
            self.assertEqual(len(progress["scene_progress"]), 12)
            self.assertTrue(all(row["status"] == "existing" for row in progress["scene_progress"]))

            jobs = []
            for index, row in enumerate(progress["scene_progress"], 1):
                jobs.append({
                    "scene_id": row["scene_id"], "part": 1,
                    "global_scene": index, "local_scene": index,
                    "scene": index, "source_scene_number": index,
                    "role": "main", "image_path": row["image_path"],
                    "prompt_signature": row["prompt_signature"],
                    "source_scene_signature": row["source_scene_signature"],
                    "source_fingerprint": source_fp,
                    "config_signature": config_sig,
                    "image_width": 720, "image_height": 1280,
                })
            def resolve_saved(relative):
                return output / relative.removeprefix("output/")
            def verify_saved(path, width, height):
                return {"sha256": hashes[Path(path).name], "integrity": "verified"}
            with patch.object(generate_visuals, "CHECKPOINT_FILE", progress_path), \
                 patch.object(generate_visuals, "VISUALS_DIR", output / "visuals"), \
                 patch.object(generate_visuals, "resolve_image_path", side_effect=resolve_saved), \
                 patch.object(generate_visuals, "validate_png", side_effect=verify_saved):
                previous = generate_visuals.load_previous_progress()
                rebuilt = generate_visuals.build_progress(jobs, previous, source_fp, config_sig)
            self.assertEqual(rebuilt["completed_jobs"], 12)
            self.assertEqual(rebuilt["remaining_jobs"], 0)

    def test_corrupt_optional_manifests_do_not_break_valid_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "visual_jobs_progress.json"
            visuals = root / "visuals"
            visuals.mkdir()
            row = {
                "scene_id": "part_01_scene_01",
                "prompt_signature": "p" * 64,
                "image_sha256": "a" * 64,
                "image_path": "output/visuals/images/scene.png",
                "source_scene_signature": "s" * 64,
                "source_fingerprint": "f" * 64,
                "config_signature": "c" * 64,
                "status": "existing",
            }
            checkpoint.write_text(
                json.dumps({"scene_progress": [row]}), encoding="utf-8"
            )
            (visuals / "image_manifest.json").write_text(
                '{"scenes": [ this is truncated', encoding="utf-8"
            )
            (visuals / "image_generation_manifest.json").write_text(
                '{"scenes": [ this is truncated', encoding="utf-8"
            )
            with patch.object(generate_visuals, "CHECKPOINT_FILE", checkpoint), \
                 patch.object(generate_visuals, "VISUALS_DIR", visuals):
                recovered = generate_visuals.load_previous_progress()
            self.assertIn((row["scene_id"], row["prompt_signature"]), recovered)

    def test_hash_mismatch_is_not_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _, _ = self._candidate(root, corrupt_hash=True, count=12)
            output = root / "output"
            self.assertEqual(
                checkpoint_manager.restore_partial_visual_images(source, output), 0
            )
            self.assertFalse(
                (output / "checkpoints" / "visual_jobs_progress.json").exists()
            )

    def test_completed_runs_include_failure_and_cancellation_conclusions(self):
        payload = json.dumps([
            {"databaseId": 11, "status": "completed", "conclusion": "success"},
            {"databaseId": 12, "status": "completed", "conclusion": "failure"},
            {"databaseId": 13, "status": "completed", "conclusion": "cancelled"},
        ])
        with patch.object(
            checkpoint_manager.subprocess, "run",
            return_value=type("Result", (), {"returncode": 0, "stdout": payload, "stderr": ""})(),
        ):
            self.assertEqual(
                checkpoint_manager.list_runs("owner/repo", "workflow.yml", "main", 100),
                [11, 12, 13],
            )


class PhotoMotionAndCaptionTests(unittest.TestCase):
    def test_enabled_parallax_uses_verified_depth_in_dynamic_style(self):
        prepared = [{
            "scene_id": "scene-1", "part": 1, "local_scene": 1,
            "duration": 6.0, "image_sha256": "a" * 64,
            "audio_sha256": "b" * 64, "role": "main",
        }]
        settings = {
            "parallax_enabled": True, "style": "dynamic",
            "variation": "high", "preserve_subject": True,
        }
        plan = photo_motion.build_effect_plan(
            prepared, {"scene-1": {"status": "generated", "sha256": "c" * 64}},
            settings,
        )
        self.assertEqual(plan["scene-1"], "parallax")

    def test_mixed_case_hinglish_is_normalized_without_losing_proper_names(self):
        self.assertEqual(
            prepare_captions.normalize_roman_case("JaISe, eXAmpLe, ADdi. Ayaan uses iPhone."),
            "Jaise, example, addi. Ayaan uses iPhone.",
        )

    def test_devanagari_english_loanwords_remain_english(self):
        converted = prepare_captions.romanize_hindi_to_hinglish("मेरा कंप्यूटर और एआई")
        self.assertIn("computer", converted)
        self.assertIn("AI", converted)

    def test_event_sound_types_include_ring_and_siren(self):
        self.assertIn("ring", generate_sound_design.SFX_TYPES)
        self.assertIn("siren", generate_sound_design.SFX_TYPES)
        self.assertIn("siren", generate_sound_design.EVENT_WORDS)
        for kind in ("vehicle", "crowd", "weather", "phone", "water", "mystery", "ring", "heartbeat", "footsteps", "door", "siren"):
            samples = list(generate_sound_design.sfx_samples(kind, duration=0.25, seed=7))
            self.assertTrue(samples)
            self.assertGreater(max(samples) - min(samples), 0.01, kind)

    def test_scene_event_text_selects_matching_sfx(self):
        examples = {
            "vehicle": "A car engine starts and the vehicle drives past.",
            "crowd": "A large crowd is talking in the street.",
            "weather": "Heavy rain and thunder begin.",
            "phone": "The phone starts ringing.",
            "water": "Water flows down the river.",
            "mystery": "A mysterious shadow appears.",
            "ring": "A bell rings loudly.",
            "heartbeat": "His heartbeat becomes faster.",
            "footsteps": "Footsteps approach through the hallway.",
            "door": "She opens the door.",
            "siren": "An ambulance siren sounds.",
        }
        for expected, prompt in examples.items():
            selected = generate_sound_design.choose_sfx(
                {"visual_prompt": prompt},
                {"role": "main", "text": prompt},
            )
            self.assertEqual(selected, expected, prompt)


class ProviderAccountingTests(unittest.TestCase):
    def test_cost_stays_unknown_without_provider_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(production_events, "EVENTS", root / "events.jsonl"), \
                 patch.object(production_events, "STATE", root / "state.json"):
                production_events.record_provider_attempt(
                    "test-provider", "test-model", "accepted", True,
                    scene_id="scene-1", attempt_id="attempt-1",
                )
                summary = production_events.provider_summary()
            self.assertEqual(summary["attempt_count"], 1)
            self.assertEqual(summary["billable_marked_attempts"], 1)
            self.assertEqual(summary["accepted_attempts"], 1)
            self.assertIsNone(summary["actual_cost_total"])
            self.assertEqual(summary["actual_cost_status"], "unknown_no_provider_receipts")


class StoryMergeEvidenceTests(unittest.TestCase):
    def test_exact_final_merge_evidence_is_required_and_verified(self):
        final_text = " ".join(f"word{i}" for i in range(192))
        digest = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
        evidence = {
            "final_part_merged": True,
            "verified": True,
            "method": "exact_adjacent_final_part_merge_from_complete_sentence_units",
            "text_preserved_exactly": True,
            "pre_merge_selected_parts": 2,
            "merge_reduced_part_count": 1,
            "final_part_count": 1,
            "final_part_number": 1,
            "final_part_word_count": 192,
            "final_part_sha256": digest,
            "corrected_story_sha256": "a" * 64,
            "reconstructed_story_sha256": "a" * 64,
        }
        self.assertTrue(verify_final_merge_evidence(
            evidence, final_text, part_number=1, actual_parts=1, policy=evidence,
        ))
        evidence["text_preserved_exactly"] = False
        self.assertFalse(verify_final_merge_evidence(
            evidence, final_text, part_number=1, actual_parts=1, policy=evidence,
        ))


class VisualQualityGateTests(unittest.TestCase):
    def test_low_vision_score_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scene.png"
            Image.effect_noise((64, 64), 100).convert("RGB").save(path)
            vision = {
                "prompt_match": True, "character_consistency": True,
                "quality_score": 6, "issues": ["soft focus"], "reason": "blur",
            }
            with patch.object(visual_quality_validator, "_vision_check", return_value=vision):
                result = visual_quality_validator.validate_scene_image(
                    path, "A clear scene-specific visual prompt for a test.",
                    (64, 64), enabled=True, minimum_score=5, api_key="test",
                )
            self.assertFalse(result["accepted"])
            self.assertEqual(result["status"], "rejected")

    def test_vision_unavailable_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scene.png"
            Image.effect_noise((64, 64), 100).convert("RGB").save(path)
            with patch.object(
                visual_quality_validator, "_vision_check",
                side_effect=visual_quality_validator.VisionVerificationUnavailable("HTTP 429"),
            ):
                with self.assertRaises(visual_quality_validator.VisionVerificationUnavailable):
                    visual_quality_validator.validate_scene_image(
                        path, "A clear scene-specific visual prompt for a test.",
                        (64, 64), enabled=True, api_key="test",
                    )


if __name__ == "__main__":
    unittest.main()
