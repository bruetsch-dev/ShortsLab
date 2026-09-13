"""When the run has a choice, it takes the clip without burned-in text.

Measured on the finished Short `in_japanese_hot_spring_towns_one_wrong` (2026-09-03): 10 scenes,
10 distinct clips, zero filler - and three of them still showed Japanese creator text at 2.8%,
4.7% and 11.9% of the frame. All three were flagged for cleaning and all three failed: the
inpainting could not rebuild the area, so the original was restored. One came out worse than it
went in (2.8% -> 5.2%) and the post-fill check correctly rejected the result.

That run had 45 distinct accepted sources for 10 beats. The cleanest fix is not a better fill -
it is to not choose that clip in the first place when 35 spares are sitting there.

The penalty only decides near-ties: at most 1.2 against a score scale where a strong metadata
match is worth 1.5, so a clean-but-wrong clip never beats a right one.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v4


def penalty_fn():
    """The shipped function, not a copy sliced out of the source file.

    This test used to read scrape_v4.py, find the helper by its indentation and exec the
    text back. It broke the moment the ranking around it changed - and a test that cannot
    survive an edit to its own subject was never testing the shipped behaviour.
    """
    return scrape_v4.text_penalty


def candidate(share, score=5.0):
    c = scrape_v4.Candidate(source_id="s", query="q", path="p", start=0.0, end=2.0,
                            score=score, reason="", status="available")
    c.caption_share = share
    return c


class CleanFootageWins(unittest.TestCase):

    def setUp(self):
        self.penalty = penalty_fn()

    def test_clean_footage_pays_nothing(self):
        self.assertEqual(self.penalty(candidate(0.0)), 0.0)
        self.assertEqual(self.penalty(candidate(0.012)), 0.0)

    def test_an_unmeasurable_clip_is_not_punished(self):
        """-1.0 means the measurement could not run; that is not evidence of text."""
        self.assertEqual(self.penalty(candidate(-1.0)), 0.0)

    def test_more_text_costs_more(self):
        self.assertLess(self.penalty(candidate(0.03)), self.penalty(candidate(0.12)))

    def test_the_penalty_cannot_outweigh_a_real_match(self):
        """A metadata match is worth up to 1.5; the text penalty tops out below that."""
        self.assertLessEqual(self.penalty(candidate(0.5)), 1.2)

    def test_a_clean_clip_beats_a_texted_one_at_the_same_score(self):
        clean, texted = candidate(0.0, 5.0), candidate(0.06, 5.0)
        self.assertGreater(5.0 - self.penalty(clean), 5.0 - self.penalty(texted))

    def test_a_strong_texted_clip_still_beats_a_weak_clean_one(self):
        strong_texted, weak_clean = candidate(0.06, 6.5), candidate(0.0, 5.0)
        self.assertGreater(6.5 - self.penalty(strong_texted), 5.0 - self.penalty(weak_clean))


if __name__ == "__main__":
    unittest.main()
