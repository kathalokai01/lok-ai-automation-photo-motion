#!/usr/bin/env python3
"""Regression tests for the SHORT final-Part merge exception."""

from __future__ import annotations

import unittest

from scripts.generate_story import _partition_short_story
from scripts.story_length_policy import word_count


def make_story(sentence_count: int, words_per_sentence: int) -> str:
    sentences = []
    for index in range(sentence_count):
        words = ["अयान"] + [f"शब्द{index}_{n}" for n in range(words_per_sentence - 1)]
        sentences.append(" ".join(words) + "।")
    return " ".join(sentences)


class ShortStoryMergeRegressionTests(unittest.TestCase):
    def test_239_exception_requires_real_short_final_part(self) -> None:
        story = make_story(10, 19)  # 190 words; can be split 133 + 57.
        groups, selected, merged = _partition_short_story(
            story,
            preferred_count=2,
            minimum_parts=1,
            maximum_parts=4,
            scenes_range=(5, 20),
        )

        self.assertTrue(merged)
        self.assertEqual(selected, 1)
        self.assertEqual(len(groups), 1)
        self.assertEqual(word_count(groups[0]), 190)
        self.assertEqual(
            " ".join(groups[0].split()),
            " ".join(story.split()),
        )

    def test_natural_single_part_over_180_is_not_a_merge(self) -> None:
        story = make_story(12, 19)  # 228 words, not a pre-existing short-final merge.
        with self.assertRaisesRegex(
            RuntimeError,
            "Could not divide the corrected story",
        ):
            _partition_short_story(
                story,
                preferred_count=1,
                minimum_parts=1,
                maximum_parts=4,
                scenes_range=(5, 20),
            )


if __name__ == "__main__":
    unittest.main()
