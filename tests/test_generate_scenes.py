#!/usr/bin/env python3
"""Regression tests for locked narration on extra scene roles."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts import checkpoint_manager
from scripts import input_config

from scripts import generate_scenes as scenes
from scripts import generate_narration as narration
from scripts import generate_story as story
from scripts import generate_story_plan as story_plan
from scripts import free_image_generator as image_generator
from scripts import generate_tts as tts
from scripts import generate_flow_music as flow_music
from unittest.mock import patch


def ai_record(scene_id: str, role: str, narration: str) -> dict:
    narration = scenes.clean(narration)
    record = {
        "scene_id": scene_id,
        "role": role,
        "narration": narration,
        "visual_prompt": "A story-specific cinematic scene with the named characters and objects.",
        "source_text_sha256": scenes.text_sha256(narration),
    }
    if role == "main":
        record["source_word_count"] = len(re.findall(r"\S+", narration))
    else:
        record["voiceover_enabled"] = True
        record["narration_source"] = "ai_generated_role_narration"
    return record


class ImageBillingScopeTests(unittest.TestCase):
    def test_pollinations_billing_block_is_scoped_to_pollinations_account(self) -> None:
        blocked = {"pollinations"}
        self.assertTrue(
            image_generator.billable_provider_is_blocked(
                "pollinations:flux.1-schnell", True, blocked
            )
        )
        self.assertTrue(
            image_generator.billable_provider_is_blocked(
                "pollinations:tongyi-mai/z-image-turbo", True, blocked
            )
        )
        self.assertFalse(
            image_generator.billable_provider_is_blocked(
                "replicate:black-forest-labs/flux-schnell", True, blocked
            )
        )
        self.assertFalse(
            image_generator.billable_provider_is_blocked(
                "cloudflare:@cf/black-forest-labs/flux-2-klein-4b",
                True,
                blocked,
            )
        )

    def test_cloudflare_primary_and_fallback_share_only_cloudflare_block(self) -> None:
        blocked = {"cloudflare"}
        self.assertTrue(
            image_generator.billable_provider_is_blocked(
                "cloudflare:@cf/black-forest-labs/flux-2-klein-4b",
                True,
                blocked,
            )
        )
        self.assertTrue(
            image_generator.billable_provider_is_blocked(
                "cloudflare:@cf/black-forest-labs/flux-1-schnell",
                True,
                blocked,
            )
        )
        self.assertFalse(
            image_generator.billable_provider_is_blocked(
                "replicate:black-forest-labs/flux-schnell",
                True,
                blocked,
            )
        )

    def test_pollinations_attempts_are_counted_as_billable(self) -> None:
        original = (
            image_generator.PROVIDER_ORDER,
            image_generator.POLLINATIONS_MODELS,
            image_generator.IMAGE_FALLBACK_ENABLED,
        )
        try:
            image_generator.PROVIDER_ORDER = ["pollinations"]
            image_generator.POLLINATIONS_MODELS = [
                "black-forest-labs/flux.1-schnell",
                "tongyi-mai/z-image-turbo",
            ]
            image_generator.IMAGE_FALLBACK_ENABLED = True
            attempts = image_generator.build_provider_attempts(
                "test prompt",
                Path("image.png"),
                (720, 1280),
                123,
                "blurry",
            )
            self.assertEqual(len(attempts), 2)
            self.assertTrue(all(row[2] is True for row in attempts))
        finally:
            (
                image_generator.PROVIDER_ORDER,
                image_generator.POLLINATIONS_MODELS,
                image_generator.IMAGE_FALLBACK_ENABLED,
            ) = original

    def test_free_provider_is_not_blocked_by_billable_provider_failure(self) -> None:
        self.assertFalse(
            image_generator.billable_provider_is_blocked(
                "huggingface:mrfakename/Z-Image-Turbo",
                False,
                {"pollinations", "replicate"},
            )
        )


class ElevenLabsPriorityTests(unittest.TestCase):
    def test_tts_primary_order_starts_with_elevenlabs(self) -> None:
        self.assertEqual(
            tts.PRIMARY_ORDER,
            ["elevenlabs", "fish_audio", "cartesia"],
        )

    def test_elevenlabs_is_available_with_profile_voice_id(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ELEVENLABS_API_KEY": "test-key",
                "ELEVENLABS_VOICE_ID_MALE_STORYTELLER": "test-voice",
            },
            clear=True,
        ):
            available, reason = tts.provider_available(
                "elevenlabs", {}, True
            )
        self.assertTrue(available, reason)

    def test_music_provider_order_starts_with_elevenlabs(self) -> None:
        self.assertEqual(
            flow_music.music_provider_order({
                "MUSIC_PROVIDER_ORDER": (
                    "eleven_music,lyria,stable_audio,local"
                )
            }),
            ["eleven_music", "lyria", "stable_audio", "local"],
        )


class TTSRoleMetadataTests(unittest.TestCase):
    def test_extra_roles_receive_required_tts_metadata(self) -> None:
        for role in ("hook", "suspense", "resolution"):
            with self.subTest(role=role):
                self.assertEqual(
                    tts.role_narration_metadata({"role": role}),
                    {
                        "voiceover_enabled": True,
                        "narration_source": "ai_generated_role_narration",
                    },
                )

    def test_main_role_keeps_locked_part_metadata(self) -> None:
        self.assertEqual(
            tts.role_narration_metadata({"role": "main"}),
            {
                "voiceover_enabled": True,
                "narration_source": "locked_part_text",
            },
        )


class ExtraSceneNarrationTests(unittest.TestCase):
    def test_hook_and_resolution_keep_their_ai_narration(self) -> None:
        main_text = "राजू ने पुराने घर का दरवाज़ा धीरे से खोला।"
        hook_text = (
            "उस रात राजू ने घर के भीतर एक ऐसी आवाज़ सुनी "
            "जिसने उसकी पूरी ज़िंदगी बदल दी।"
        )
        resolution_text = (
            "सुबह होते ही राजू को उस रहस्य का सच पता चल गया "
            "और परिवार फिर से सुरक्षित हो गया।"
        )
        main = ai_record("part_01_scene_01", "main", main_text)
        hook = ai_record("part_01_hook", "hook", hook_text)
        resolution = ai_record(
            "part_01_resolution", "resolution", resolution_text
        )
        story = {
            "status": "completed",
            "format": "short",
            "total_parts": 1,
            "total_scenes": 1,
            "total_extra_scenes": 2,
            "parts": [{
                "part": 1,
                "narration": main_text,
                "text_locked": True,
                "locked_text_sha256": scenes.text_sha256(main_text),
                "scenes": [main],
                "hook_scene": hook,
                "suspense_scene": None,
                "resolution_scene": resolution,
            }],
        }

        parts, expected_items, main_total = scenes.validate_story(
            story,
            {"format": "short"},
            (1, 1),
        )

        self.assertEqual(len(parts), 1)
        self.assertEqual(main_total, 1)
        by_id = {item["scene_id"]: item for item in expected_items}
        self.assertEqual(by_id["part_01_hook"]["narration"], hook_text)
        self.assertEqual(
            by_id["part_01_resolution"]["narration"], resolution_text
        )


    def test_scene_progress_checkpoint_is_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "output"
            checkpoint = source / "checkpoints" / "scenes_progress.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text(
                json.dumps({
                    "version": "photo-motion-scenes-v11-extra-role-narration-fix",
                    "status": "in_progress",
                    "generation_signature": "b" * 64,
                    "completed_scene_ids": ["part_01_scene_01"],
                }),
                encoding="utf-8",
            )

            copied = checkpoint_manager.merge_progress_checkpoints(
                source, output
            )

            restored = output / "checkpoints" / "scenes_progress.json"
            self.assertEqual(copied, 1)
            self.assertTrue(restored.is_file())
            self.assertEqual(
                json.loads(restored.read_text(encoding="utf-8"))[
                    "generation_signature"
                ],
                "b" * 64,
            )



    def test_story_scene_and_narration_use_identical_word_count(self) -> None:
        # An isolated em dash is a whitespace token in Story partitioning.
        # Regex word-boundary counting previously discarded it in narration.
        sample = "राजू ने कहा — ‘रुको!’ फिर वह लौट आया।"
        expected = len(re.findall(r"\S+", re.sub(r"\s+", " ", sample).strip()))
        self.assertEqual(story.word_count(sample), expected)
        self.assertEqual(scenes.count_narration_words(sample), expected)
        self.assertEqual(narration.count_words(sample), expected)

    def test_extra_scene_word_count_uses_narration_validator_rule(self) -> None:
        # All pipeline stages now use whitespace-token counting.
        sample = "राजू ने कहा — ‘रुको!’ फिर वह लौट आया।"
        expected = narration.count_words(sample)
        self.assertEqual(scenes.count_narration_words(sample), expected)

        extra = {
            "part": 1,
            "scene": None,
            "scene_id": "part_01_hook",
            "role": "hook",
            "narration": sample,
            "source": {"story_anchor": "रात का रहस्य"},
        }
        record = {
            "status": "completed",
            "part": 1,
            "scene": None,
            "scene_id": "part_01_hook",
            "role": "hook",
            "narration": sample,
            "source_scene_id": "part_01_hook",
            "source_role": "hook",
            "source_scene_signature": scenes.expected_source_signature(extra),
            "generation_signature": "c" * 64,
            "scene_prompt_version": scenes.PROMPT_VERSION,
            "is_extra_scene": True,
            "visual_prompt": "A dark house at night.",
            "negative_prompt": "blurry, distorted",
            "camera_prompt": "slow push in",
            "lighting_prompt": "moonlight",
            "voiceover_enabled": True,
            "narration_source": "ai_generated_role_narration",
            "source_text_sha256": scenes.text_sha256(sample),
            "source_word_count": expected,
        }
        self.assertTrue(scenes.scene_is_valid(record, extra, "c" * 64))
        # Reproduce the old Unicode word-boundary counter, which drops the isolated em dash token.
        record["source_word_count"] = len(
            re.findall(r"\b[\w'-]+\b", sample, flags=re.UNICODE)
        )
        self.assertNotEqual(record["source_word_count"], expected)
        self.assertFalse(scenes.scene_is_valid(record, extra, "c" * 64))

    def test_countdown_fragments_are_not_separate_scenes(self) -> None:
        sample = (
            "अयान ने पैच लिखा। आखिरी 10 सेकंड... 9... 8... "
            "उसने एंटर दबाया।"
        )
        expected = [
            "अयान ने पैच लिखा।",
            "आखिरी 10 सेकंड... 9... 8...",
            "उसने एंटर दबाया।",
        ]
        self.assertEqual(story_plan.split_sentences(sample), expected)
        self.assertEqual(narration.sentence_units(sample), expected)
        self.assertTrue(all(len(unit.split()) > 1 for unit in expected))

    def test_part_starting_inside_dialogue_does_not_merge_remaining_sentences(self) -> None:
        # This Part starts inside dialogue opened in the previous Part.
        # Its first quote is therefore a closing quote, not an opening quote.
        sample = (
            'अयान सर, बचा लीजिए उन्हें, आपके अलावा कोई इस सिस्टम को '
            'रीबूट नहीं कर सकता!" अयान के पैरों तले जमीन खिसक गई। '
            "जिस भूल को उसने पैसों के लिए छिपाया था, वही भूल अब उसकी "
            "अपनी दुनिया को तबाह करने जा रही थी। "
            "विक्रांत उसे धोखा देकर भाग चुका था। "
            "वह पागलों की तरह सीढ़ियों की तरफ भागा। "
            "45वीं मंजिल से नीचे की तरफ दौड़ते हुए उसका दम फूल रहा था। "
            "हर कदम के साथ उसे विक्रांत के वे 5 मिलियन डॉलर याद आ रहे थे, "
            "जो अब उसे कागज के टुकड़ों से ज्यादा कुछ नहीं लग रहे थे। "
            "वह हांफते हुए 42वें फ्लोर के सर्वर रूम में पहुंचा।"
        )
        plan_units = story_plan.split_sentences(sample)
        narration_units = narration.sentence_units(sample)

        self.assertEqual(plan_units, narration_units)
        self.assertGreaterEqual(len(plan_units), 7)
        self.assertIn("वह हांफते हुए 42वें फ्लोर", plan_units[-1])
        self.assertEqual(" ".join(plan_units), sample)


    def test_quoted_dialogue_and_attribution_are_one_scene_unit(self) -> None:
        sample = (
            "स्लैब विक्रांत के पैरों पर आ गिरा। "
            '"बचाओ! कोई है?" विक्रांत दर्द से चीख पड़ा। '
            "लेकिन वहां सिर्फ तूफान की आवाज थी।"
        )
        expected = [
            "स्लैब विक्रांत के पैरों पर आ गिरा।",
            '"बचाओ! कोई है?" विक्रांत दर्द से चीख पड़ा।',
            "लेकिन वहां सिर्फ तूफान की आवाज थी।",
        ]
        self.assertEqual(story_plan.split_sentences(sample), expected)
        self.assertEqual(narration.sentence_units(sample), expected)
        self.assertTrue(all(len(unit.split()) > 3 for unit in expected))
        self.assertEqual(" ".join(expected), sample)


    def test_long_quoted_dialogue_keeps_complete_sentence_boundaries(self) -> None:
        base = "राम सुबह पुराने शहर की शांत सड़क पर धीरे धीरे आगे बढ़ता रहा।"
        dialogue = (
            '"पहला संवाद वाक्य अयान ने शांत कमरे में धीरे अपना संदेश पूरा किया। '
            'दूसरा संवाद वाक्य उसने अपने परिवार को सुरक्षित रखने का संकल्प दोहराया था। '
            'तीसरा संवाद वाक्य अयान ने कठिन समय में जिम्मेदारी निभाने की बात कही।" '
            "अयान ने शांत स्वर में कहा।"
        )
        complete_story = " ".join([base] * 9 + [dialogue] + [base] * 90)

        plan_units = story_plan.split_sentences(complete_story)
        narration_units = narration.sentence_units(complete_story)
        self.assertEqual(plan_units, narration_units)
        self.assertTrue(
            any("दूसरा संवाद वाक्य" in unit for unit in plan_units)
        )
        self.assertEqual(" ".join(plan_units), complete_story)

        parts, selected_count, merged = story._partition_short_story(
            complete_story,
            preferred_count=10,
            minimum_parts=7,
            maximum_parts=11,
            scenes_range=(7, 15),
        )
        self.assertEqual(selected_count, 10)
        self.assertEqual(len(parts), 10)
        self.assertFalse(merged)
        self.assertEqual(story.clean(" ".join(parts)), story.clean(complete_story))
        self.assertTrue(
            all(120 <= story.word_count(part) <= 150 for part in parts)
        )

    def test_extra_scene_rejects_missing_narration(self) -> None:
        expected = {
            "part": 1,
            "scene": None,
            "scene_id": "part_01_hook",
            "role": "hook",
            "narration": "कम से कम आठ शब्दों का पूरा हुक वाक्य यहाँ होना चाहिए।",
            "source": {"story_anchor": "रात का रहस्य"},
        }
        invalid_scene = {
            "status": "completed",
            "part": 1,
            "scene": None,
            "scene_id": "part_01_hook",
            "role": "hook",
            "narration": "",
            "source_scene_id": "part_01_hook",
            "source_role": "hook",
            "source_scene_signature": scenes.expected_source_signature(expected),
            "generation_signature": "a" * 64,
            "scene_prompt_version": scenes.PROMPT_VERSION,
            "is_extra_scene": True,
            "visual_prompt": "A dark house at night.",
            "negative_prompt": "blurry, distorted",
            "camera_prompt": "slow push in",
            "lighting_prompt": "moonlight",
            "voiceover_enabled": True,
            "narration_source": "ai_generated_role_narration",
            "source_text_sha256": scenes.text_sha256(expected["narration"]),
        }

        self.assertFalse(
            scenes.scene_is_valid(invalid_scene, expected, "a" * 64)
        )


class FreeFirstImageProviderOrderTests(unittest.TestCase):
    def test_configured_provider_order_is_exact_and_complete(self):
        config = input_config.load_and_validate(
            Path(__file__).resolve().parents[1] / "Input" / "topic.txt"
        )
        self.assertEqual(
            input_config.get_image_provider_order(config),
            [
                "huggingface",
                "cloudflare_schnell",
                "ovhcloud",
                "aihorde",
                "pollinations",
                "cloudflare_klein",
                "replicate",
            ],
        )

    def test_provider_aliases_resolve_without_reordering(self):
        config = {"IMAGE_PROVIDER_ORDER": "hf,ovh,ai_horde,cloudflare_schnell"}
        self.assertEqual(
            input_config.get_image_provider_order(config),
            ["huggingface", "ovhcloud", "aihorde", "cloudflare_schnell"],
        )


class SceneTransitionDefaultTests(unittest.TestCase):
    def _build(self, source):
        settings = scenes.build_settings({"FORMAT": "short"})
        item = {
            "source": {
                "scene_id": "part_01_scene_01",
                "role": "main",
                "visual_prompt": "A detailed story-specific cinematic scene with a named character.",
                **source,
            },
            "part": 1,
            "scene": 1,
            "role": "main",
            "scene_id": "part_01_scene_01",
            "part_data": {"scene_duration": "auto"},
            "narration": "कहानी का पूरा वाक्य।",
            "global_scene_number": 1,
        }
        with (
            patch.object(scenes, "scene_character_references", return_value=[]),
            patch.object(scenes, "find_character_records", return_value=[]),
            patch.object(
                scenes,
                "build_visual_prompt",
                return_value="A detailed story-specific cinematic still image prompt.",
            ),
        ):
            return scenes.build_scene_record(
                item, settings, {"world": {}}, None
            )

    def test_missing_transition_uses_neutral_cut_without_input_setting(self):
        record = self._build({})
        self.assertEqual(record["transition"], "cut")

    def test_explicit_source_transition_is_preserved(self):
        record = self._build({"transition": "crossfade"})
        self.assertEqual(record["transition"], "crossfade")



if __name__ == "__main__":
    unittest.main()
