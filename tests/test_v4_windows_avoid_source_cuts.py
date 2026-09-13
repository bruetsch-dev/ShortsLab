"""A beat is one shot, so its window must not contain the source's own cuts.

Measured on the eating-walk Short, inside the seconds each beat actually shows:

    our own cuts                  13
    cuts inside the source clips  13
    shots the viewer sees         27, where the edit planned 14

Scene 10 carried nine cuts in 2.88s - a single "shot" that is really a ten-shot montage. That
is what "too many cuts, and cuts from the original video" describes, and no work on our own cut
points can undo it.

scrape_v2 has always picked windows between the cuts. scrape_v4 - the engine that runs - never
called hard_cut_times once: _windows placed its candidates at a fixed 8%, 49% and 80% of the
source, blind to what happens there.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v4


class CleanGaps(unittest.TestCase):
    def test_a_source_with_no_cuts_is_one_long_gap(self):
        self.assertEqual(scrape_v4._clean_gaps(10.0, [], 2.0), [(0.0, 10.0)])

    def test_cuts_split_the_source(self):
        gaps = scrape_v4._clean_gaps(10.0, [3.0, 6.0], 2.0)
        self.assertEqual(gaps, [(0.0, 3.0), (3.0, 6.0), (6.0, 10.0)])

    def test_a_gap_too_short_for_the_beat_is_not_offered(self):
        """Nine cuts in three seconds leaves nothing a two-second beat can sit in."""
        gaps = scrape_v4._clean_gaps(3.0, [0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.4, 2.7], 2.0)
        self.assertEqual(gaps, [])

    def test_cuts_outside_the_clip_are_ignored(self):
        self.assertEqual(scrape_v4._clean_gaps(5.0, [-1.0, 9.0], 2.0), [(0.0, 5.0)])


class WindowsAvoidTheCuts(unittest.TestCase):
    def test_without_cuts_the_old_placement_is_unchanged(self):
        """The common case must not move: this runs on every source in every scrape."""
        self.assertEqual(scrape_v4._windows(30.0, 2.0),
                         scrape_v4._windows(30.0, 2.0, cuts=None))

    def test_a_window_slides_out_of_a_cut(self):
        """8% of 30s is 2.4s, which sits right on a cut at 3.0s for a 2s beat."""
        blind = scrape_v4._windows(30.0, 2.0)
        aware = scrape_v4._windows(30.0, 2.0, cuts=[3.0])
        self.assertTrue(aware)
        for s, e in aware:
            self.assertFalse(s < 3.0 < e, "a window still straddles the cut: %.2f-%.2f" % (s, e))

    def test_every_window_lands_in_a_clean_stretch(self):
        cuts = [4.0, 9.0, 14.5, 21.0]
        for s, e in scrape_v4._windows(30.0, 2.0, cuts=cuts):
            self.assertFalse(any(s < c < e for c in cuts), "%.2f-%.2f contains a cut" % (s, e))

    def test_a_source_that_cuts_constantly_is_not_offered_as_one_shot(self):
        """A rapid montage cannot masquerade as a clean candidate window."""
        cuts = [x * 0.4 for x in range(1, 70)]
        self.assertEqual([], scrape_v4._windows(28.0, 2.0, cuts=cuts))

    def test_windows_stay_inside_the_clip(self):
        for s, e in scrape_v4._windows(12.0, 2.5, cuts=[1.0, 11.5]):
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(e, 12.0)


class TheScrapeActuallyMeasuresIt(unittest.TestCase):
    def setUp(self):
        with open(scrape_v4.__file__, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_cuts_are_scanned_per_downloaded_source(self):
        self.assertIn("clip_scraper.hard_cut_times(", self.src)

    def test_they_are_handed_to_the_window_chooser(self):
        self.assertIn("_windows(duration, per_clip_seconds, cuts=_cuts,", self.src)
        self.assertIn("longest=_longest_beat", self.src,
                      "windows are capped at the cadence again, so a long beat has no candidate")

    def test_a_window_that_still_cuts_scores_lower(self):
        self.assertIn("5.0 + popularity - 0.6 * inside", self.src)

    def test_and_says_so_in_its_own_reason(self):
        self.assertIn("the source cuts {inside}x", self.src)


class GrowingAWindowStopsAtTheCut(unittest.TestCase):
    """The hole the first version of this fix left open.

    Choosing a window inside a clean stretch is only half of "a beat is one shot". A beat longer
    than the 2.3s excerpt makes the fit pass EXTEND the window rather than slow the clip down -
    and that pass measured only how much source was left, not where the next cut was. A 2.3s
    window chosen 1.5s before a cut, asked to cover a 6s beat, would have run four and a half
    seconds past it.
    """

    def test_the_window_remembers_where_its_shot_ends(self):
        self.assertEqual(5.5, scrape_v4._next_cut_after(3.0, [1.0, 5.5, 9.0], 20.0))
        self.assertEqual(9.0, scrape_v4._next_cut_after(6.0, [1.0, 5.5, 9.0], 20.0))

    def test_a_source_with_no_cuts_may_grow_to_its_own_end(self):
        self.assertEqual(20.0, scrape_v4._next_cut_after(3.0, [], 20.0))

    def test_a_cut_at_the_window_edge_is_not_read_as_room(self):
        """Within 50ms of the end is the same cut, not the next one."""
        self.assertEqual(9.0, scrape_v4._next_cut_after(5.48, [5.5, 9.0], 20.0))

    def test_fit_does_not_extend_beyond_visually_reviewed_seconds(self):
        source = inspect.getsource(scrape_v4.scrape_social_plan_v4)
        self.assertNotIn("chosen.end = round", source)
        self.assertIn('copy["reviewed_window"]', source)

    def test_a_candidate_carries_it(self):
        self.assertIn("clean_until", scrape_v4.Candidate.__dataclass_fields__)
        self.assertEqual(0.0, scrape_v4.Candidate.__dataclass_fields__["clean_until"].default)


if __name__ == "__main__":
    unittest.main()
