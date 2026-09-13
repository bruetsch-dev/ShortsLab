"""A window where nothing moves is a photograph with a caption on it.

The owner's word for it is "Standbilder", and the finished videos measure him out: frame-to-frame
difference across whole renders (2026-09-05) puts 26.4% of the dating Short below 2.5 grey levels
of movement, against 1.4% of the konbini one. Nothing in V4 asked the question - the vision pass
rates *interest*, which is an impression of the subject, not of whether the picture moves.

So the review strip, which is decoded anyway, now also answers it: the smaller of the two
differences between its thirds. Measured over the windows those two Shorts actually used, and over
the pool they were chosen from:

    konbini, assigned      n=10   min 26.5   p10 29.7   median 48.1   max 73.1
    konbini, available     n=40   min  4.3   p10 11.7   median 38.2   max 69.3
    couples, assigned      n=10   min  8.2   p10 15.2   median 24.6   max 43.4
    couples, available     n=40   min  4.0   p10 13.7   median 32.4   max 61.3

MIN_WINDOW_MOTION = 9.0 therefore sits below the tenth percentile of what was good enough to be
assigned and above the still end of the pool. Above the gate it is a tiebreaker only: it can shift
a candidate by at most one point, which is one point of relevance - it must never buy a lively shot
past a shot that carries the line.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v4

BODY = inspect.getsource(scrape_v4)


class _Candidate:
    def __init__(self, motion=30.0, relevance=9, interest=6.0, dead=0.0):
        self.motion = motion
        self.vision = {"relevance": relevance}
        self.visual_interest = interest
        self.dead_share = dead
        self.caption_share = 0.0


class TheFloor(unittest.TestCase):
    def test_it_is_where_the_measurement_put_it(self):
        self.assertEqual(scrape_v4.MIN_WINDOW_MOTION, 9.0)

    def test_a_still_window_is_rejected_in_the_review_pass(self):
        self.assertIn("if 0.0 <= candidate.motion < MIN_WINDOW_MOTION:", BODY)
        self.assertIn("nothing moves in this window", BODY)

    def test_an_unmeasured_window_is_not_punished(self):
        """-1.0 means "could not measure", and that must not read as "does not move"."""
        self.assertIn("motion: float = -1.0", BODY)
        self.assertLess(-1.0, scrape_v4.MIN_WINDOW_MOTION)
        # the gate only fires on a measured value
        self.assertIn("0.0 <= candidate.motion", BODY)
        self.assertAlmostEqual(scrape_v4.assignment_rank(5.0, _Candidate(motion=-1.0)),
                               scrape_v4.assignment_rank(5.0, _Candidate(motion=-1.0)))


class TheTiebreaker(unittest.TestCase):
    def test_a_lively_window_outranks_a_barely_moving_one_that_fits_the_same(self):
        lively = scrape_v4.assignment_rank(5.0, _Candidate(motion=48.0))
        static = scrape_v4.assignment_rank(5.0, _Candidate(motion=10.0))
        self.assertGreater(lively, static)
        self.assertLess(lively - static, 1.0)

    def test_it_can_never_outweigh_relevance(self):
        """One point of relevance above the gate is worth 0.8; movement is capped at 0.8 too."""
        dull_but_right = scrape_v4.assignment_rank(5.0, _Candidate(motion=9.5, relevance=10))
        lively_but_worse = scrape_v4.assignment_rank(5.0, _Candidate(motion=80.0, relevance=9))
        self.assertGreater(dull_but_right, lively_but_worse)

    def test_the_hook_weighs_movement_more(self):
        hook = scrape_v4.assignment_rank(5.0, _Candidate(motion=45.0), is_hook=True)
        body = scrape_v4.assignment_rank(5.0, _Candidate(motion=45.0))
        rest_hook = scrape_v4.assignment_rank(5.0, _Candidate(motion=0.0), is_hook=True)
        rest_body = scrape_v4.assignment_rank(5.0, _Candidate(motion=0.0))
        self.assertGreater(hook - rest_hook, body - rest_body)


if __name__ == "__main__":
    unittest.main()
