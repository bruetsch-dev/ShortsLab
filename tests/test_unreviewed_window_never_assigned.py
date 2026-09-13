"""A window nobody looked at may not carry a beat.

Measured on the couples Short of 2026-09-05 (`projects/_old_couples_before_window_review`): ten
windows were assigned, and the per-window vision file holds a verdict for nine of them -

    ring on a finger over Tokyo Tower            rel 10
    boy pats a girl's head at a station          rel  9
    phone case with couple photos                rel 10
    couple walking through an illumination tunnel rel 10
    couple at a window, Tokyo Tower at night     rel  7
    two people embracing outside a JR station    rel  8
    couple walking down a park path              rel  8
    couple in front of illumination trees        rel  9
    two hands showing matching rings             rel 10

- and NO verdict at all for the tenth. That tenth window is the shot of a wooden mallet standing
in a yard, which the finished video shows at 6.3s under the word "COUPLES".

How it got in: every relevance test reads a missing verdict as 0 and refuses it, but the metadata
path never tests relevance. A window whose POST CAPTION mentioned couples could win the beat
without a single frame of it having been looked at. So the assignment now drops unreviewed windows
before ranking, and says how many it dropped.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v4

BODY = inspect.getsource(scrape_v4)


class UnreviewedWindows(unittest.TestCase):
    def test_they_are_dropped_before_the_ranking(self):
        self.assertIn("unreviewed = [cand for cand in matched if not (cand.vision or {})]", BODY)
        self.assertIn('matched = [cand for cand in matched if (cand.vision or {})]', BODY)

    def test_the_drop_happens_before_a_candidate_can_be_ranked(self):
        drop = BODY.index("unreviewed = [cand for cand in matched")
        rank = BODY.index("ranked = []\n            for cand in matched:")
        self.assertLess(drop, rank)

    def test_the_run_says_it_dropped_them(self):
        self.assertIn("were never ", BODY)
        self.assertIn("not assignable", BODY)

    def test_the_reason_is_written_on_the_candidate(self):
        """The report is the only place this is ever explained after the fact."""
        self.assertIn('cand.rejection = "never reviewed', BODY)


class TheMeasurementItCameFrom(unittest.TestCase):
    """Guard the numbers above so a later change cannot quietly make them wrong."""

    def test_relevance_missing_reads_as_zero_everywhere_else(self):
        # every other gate already refuses an empty verdict; this is what made the hole so quiet
        self.assertIn('float(verdict.get("relevance") or 0) < 8', BODY)
        self.assertIn('float(verdict.get("relevance") or 0) >= 7', BODY)


if __name__ == "__main__":
    unittest.main()
