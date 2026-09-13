"""V4's final picker ranks alternatives but the live edit accepts only verified beats.

Requested 2026-09-02: a beat with no footage of its own should take the best thing the run found
and be outlined in red in the timeline editor until it is replaced - not abort the whole Short.

The ranking cases below still protect source/window/length behavior. The production call passes
the scene contract; if no ranked alternative proves it, the run reports the gap and stops.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v4


class FillsEveryBeat(unittest.TestCase):

    def setUp(self):
        with open(scrape_v4.__file__, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_the_fill_is_no_longer_capped_at_two_beats(self):
        self.assertNotIn("len(_uncovered) <= COVERAGE_FILL_LIMIT", self.source)
        self.assertIn("if _uncovered:", self.source)

    def test_the_shelf_is_ordered_by_evidence(self):
        """This run's own verdict first, then text, then interest, then vision relevance.

        Superseded 2026-09-04: the order used to lead with the model's `accept` flag, which is
        what the reviewer CLAIMED about the picture. A window carrying 18% burned-in creator
        text - rejected by this run for exactly that - was accepted by the model, so it outranked
        two approved windows of the same post at 8% and 2% and went into the couples Short. The
        comparator now leads with `status`, the run's own conclusion after measuring, and it
        moved to module level so it can be exercised directly (tests/test_fill_prefers_approved).
        """
        import scrape_v4
        self.assertIn("def fill_rank(candidate):", self.source)
        self.assertIn('1 if candidate.status == "available" else 0', self.source)
        self.assertIn("key=fill_rank, reverse=True", self.source)

        def make(status, relevance, share):
            candidate = scrape_v4.Candidate("s", "q", "p", 0.0, 2.75, 5.0, "", status)
            candidate.vision = {"accept": True, "relevance": relevance}
            candidate.caption_share = share
            return candidate

        self.assertGreater(scrape_v4.fill_rank(make("available", 7, 0.08)),
                           scrape_v4.fill_rank(make("rejected", 8, 0.18)))

    def test_the_fill_order_runs_on_topic_first_then_unused_then_anything(self):
        """Changed after the mirrors Fact Short: preferring an UNUSED source meant preferring a
        clip found for a different sentence over a second moment of this beat's own footage.

        The picking moved into _pick_coverage_fill when the fill was taught about fingerprints,
        so this now exercises the order instead of reading it out of the source text.
        """
        def cand(source_id, query, fingerprint):
            return scrape_v4.Candidate(source_id, query, f"{source_id}.mp4", 0.0, 2.75, 5.0,
                                       "", "available", fingerprint=fingerprint)

        own = cand("own", "this beat's query", "1111000011110000")
        unused = cand("unused", "another beat's query", "2222000022220000")
        leftover = cand("leftover", "another beat's query", "3333000033330000")
        wanted = {"this beat's query"}
        # 1. the beat's own footage
        pick, _repeat = scrape_v4._pick_coverage_fill([unused, own], wanted, {}, [], [])
        self.assertEqual(pick.source_id, "own")
        # 2. with nothing of its own left, a source nobody is using
        pick, _repeat = scrape_v4._pick_coverage_fill(
            [leftover, unused], wanted, {"leftover": 1}, [("leftover", 0.0, 2.75)], [])
        self.assertEqual(pick.source_id, "unused")
        # 3. and only then anything that does not repeat a window already on screen
        pick, _repeat = scrape_v4._pick_coverage_fill(
            [leftover], wanted, {"leftover": 1}, [("leftover", 20.0, 22.75)], [])
        self.assertEqual(pick.source_id, "leftover")

    def test_an_empty_or_partially_unverified_run_fails(self):
        self.assertIn("if not covered:", self.source)
        self.assertIn("V4 found no usable footage at all for this script", self.source)
        self.assertIn("if missing:", self.source)
        self.assertIn("V4 editorial contract failed", self.source)

    def test_a_fill_is_a_match_only_after_line_specific_evidence(self):
        self.assertIn('need=_beat_needs, scene=out_scenes[index])', self.source)
        self.assertIn('"assignment_type": "v4_verified_fill", "match_class": "vision_matched",',
                      self.source)


if __name__ == "__main__":
    unittest.main()


class TheFillStillCannotInventFootage(unittest.TestCase):
    """Filling every beat must not mean conjuring a tile out of nothing."""

    def setUp(self):
        with open(scrape_v4.__file__, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_only_files_that_actually_downloaded_reach_the_shelf(self):
        self.assertIn('Path(str(c.path or "")).is_file()', self.source)

    def test_a_beat_with_nothing_left_is_skipped_rather_than_faked(self):
        self.assertIn("if pick is None:\n                    continue", self.source)


class AFillPrefersTheBeatsOwnFootage(unittest.TestCase):
    """Measured on the mirrors Fact Short: 7 distinct accepted sources for 14 beats.

    Eight beats were filled from the leftover pile - shots that had been found for a DIFFERENT
    sentence - and the finished video read as unrelated footage over the narration. A second,
    well-separated moment from a source THIS beat's own query returned is still about this beat.
    """

    def setUp(self):
        with open(scrape_v4.__file__, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_the_beats_own_source_is_tried_first(self):
        """A stranger with a better score still loses to footage this beat's own query found."""
        stranger = scrape_v4.Candidate("stranger", "another beat", "s.mp4", 0.0, 2.75, 9.0,
                                       "", "available", fingerprint="aaaa0000aaaa0000")
        own = scrape_v4.Candidate("own", "this beat", "o.mp4", 0.0, 2.75, 1.0,
                                  "", "available", fingerprint="bbbb0000bbbb0000")
        pick, _repeat = scrape_v4._pick_coverage_fill([stranger, own], {"this beat"}, {}, [], [])
        self.assertEqual(pick.source_id, "own")

    def test_the_query_set_is_keyed_by_position_not_scene_index(self):
        """out_scenes is built in task order; keying by the original scene index misaligns it."""
        self.assertIn("_wanted_by_position = {pos: {q.casefold() for q in qs}", self.source)

    def test_a_second_window_must_not_overlap_one_already_on_screen(self):
        self.assertIn("cand.end <= a - .45 or cand.start >= b + .45", self.source)


class LongSourcesOfferMoreThanTwoMoments(unittest.TestCase):
    """A 30s post used to offer two windows, at 8% and 49%."""

    def test_a_long_source_offers_three_separated_windows(self):
        windows = scrape_v4._windows(30.0, 2.3)
        self.assertEqual(len(windows), 3)
        gaps = [windows[i + 1][0] - windows[i][0] for i in range(len(windows) - 1)]
        self.assertTrue(all(g >= 3.0 for g in gaps), f"windows too close together: {gaps}")

    def test_a_short_source_still_offers_what_it_can(self):
        for duration in (2.0, 5.0, 9.0):
            windows = scrape_v4._windows(duration, 2.3, cuts=[])
            self.assertTrue(windows)
            self.assertTrue(all(0 <= a < b <= duration for a, b in windows))

    def test_the_windows_stay_inside_the_source(self):
        for duration in (5.0, 9.0, 14.0, 30.0, 60.0):
            for start, end in scrape_v4._windows(duration, 2.3):
                self.assertGreaterEqual(start, 0.0)
                self.assertLessEqual(end, duration)
