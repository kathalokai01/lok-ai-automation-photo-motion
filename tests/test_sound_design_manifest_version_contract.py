from pathlib import Path
import ast
import math
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_sound_design.py"
VALIDATOR = ROOT / "scripts" / "validate_sfx_timing.py"


def assigned_string(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
    raise AssertionError(f"Missing string constant: {name}")


class SoundDesignManifestVersionContractTests(unittest.TestCase):
    def test_validator_accepts_exact_generator_manifest_version(self):
        generator_tree = ast.parse(GENERATOR.read_text(encoding="utf-8"))
        validator_tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
        generated_version = assigned_string(generator_tree, "VERSION")
        accepted_version = assigned_string(validator_tree, "SOUND_DESIGN_VERSION")
        self.assertEqual(accepted_version, generated_version)

    def test_fire_sfx_has_dedicated_peak_safe_profile(self):
        generator_tree = ast.parse(GENERATOR.read_text(encoding="utf-8"))
        source = GENERATOR.read_text(encoding="utf-8")
        self.assertIn('if kind == "fire":', source)
        self.assertIn('"fire-peak-safe-v3"', source)
        self.assertIn('signature_data["normalization_profile"]', source)
        self.assertIn('loudnorm=I=-16:TP=-6.0:LRA=8', source)
        self.assertNotIn('loudnorm=I=-16:TP=-3.0:LRA=8', source)
        self.assertNotIn('"legacy-sfx-profile-v1"', source)
        self.assertIn('alimiter=limit=0.45:attack=5:release=100:level=disabled', source)
        self.assertIn('"sfx_sample_peak_ceiling_dbfs": -0.1', source)
        self.assertIn('"fire_sfx_sample_peak_ceiling_dbfs": -2.5', source)
        self.assertIn('"fire_sfx_limiter_linear_ceiling": 0.45', source)
        self.assertIn('Generated fire SFX exceeded the dedicated -2.5 dBFS ', source)
        self.assertIn('sample-peak ceiling after conservative normalization and limiting.', source)
        self.assertNotIn('Generated fire SFX failed its dedicated peak ceiling', source)
        self.assertNotIn('Generated fire SFX failed its dedicated peak ceiling', source)

    def test_validator_version_is_explicitly_versioned(self):
        validator_tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
        validator_version = assigned_string(validator_tree, "VERSION")
        self.assertIn("sfx-validator-v17-", validator_version)


    def test_fire_filter_keeps_generated_wav_inside_peak_and_audibility_limits(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg is not installed")

        rate = 22050
        duration = 8
        rng = random.Random(123456)
        noise_state = 0.0
        samples = []

        for index in range(rate * duration):
            timestamp = index / rate
            noise = rng.uniform(-1.0, 1.0)
            crack = 1.0 if rng.random() < 0.0018 else 0.0
            noise_state = 0.88 * noise_state + 0.12 * noise
            fade = min(
                1.0,
                timestamp / 0.025,
                (duration - timestamp) / 0.45,
            )
            value = (
                0.16 * noise_state
                + crack * noise * 0.9
                + 0.06 * math.sin(2 * math.pi * 72 * timestamp)
            ) * max(0.0, fade)
            samples.append(max(-0.92, min(0.92, value)))

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "fire-source.wav"
            output = Path(temporary) / "fire-limited.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(rate)
                audio.writeframes(
                    b"".join(
                        struct.pack("<h", int(sample * 32767))
                        for sample in samples
                    )
                )

            subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source),
                    "-af",
                    "loudnorm=I=-16:TP=-6.0:LRA=8,"
                    "alimiter=limit=0.45:attack=5:release=100:level=disabled",
                    "-ac", "1", "-ar", str(rate),
                    "-c:a", "pcm_s16le", str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            measured = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-i", str(output),
                    "-af", "volumedetect", "-f", "null", "-",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            report = measured.stderr
            mean_line = next(
                line for line in report.splitlines()
                if "mean_volume:" in line
            )
            peak_line = next(
                line for line in report.splitlines()
                if "max_volume:" in line
            )
            mean_db = float(mean_line.split("mean_volume:", 1)[1].split()[0])
            peak_db = float(peak_line.split("max_volume:", 1)[1].split()[0])

            self.assertGreaterEqual(mean_db, -25.0)
            self.assertLessEqual(peak_db, -2.5)



if __name__ == "__main__":
    unittest.main()
