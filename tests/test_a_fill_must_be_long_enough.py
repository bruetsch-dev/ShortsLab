"""The matched path refused a window shorter than its beat. The fill path never asked.

A V4 window is cut to hold ONE shot: it ends where the source's next hard cut is. So a beat that
plays past its window plays straight through that cut - the exact thing choosing the window was
careful to avoid. The assignment loop has always enforced this (`V4_STRETCH_FLOOR`);
`_pick_coverage_fill` had no length test of any kind, and coverage fills are where most beats of
a thin run end up.

Measured on the eating-walk Short (2026-09-09): 8 of 15 scenes trip `hidden_source_cut` at their
saved in-points, and every one is a coverage fill whose window ends exactly at the cut
(clean_until 1.233 / 3.6 / 6.667 / 9.033 / 11.6 / 15.1 / 17.267 / 28.633) while the scene plays
on past it - scene 09's window is 5.95-6.62 and its beat runs 5.95-8.43.

A hole is still worse than a stretched shot, so the length test is a preference, not a veto: the
three tiers run again without it before anything is left uncovered.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v4


class Candidate:
    def __init__(self, source_id, start, end, query="q", status="available"):
        self.source_id = source_id
        self.start = start
        self.end = end
        self.query = query
        self.status = status
        self.fingerprint = ""
        self.window_fingerprint = ""


def pick(shelf, need, wanted=None, used_sources=None):
    return scrape_v4._pick_coverage_fill(shelf, wanted or set(), used_sources or {}, [], [],
                                         need=need)


class ALongEnoughWindowWins(unittest.TestCase):
    def test_a_short_window_is_passed_over_for_one_that_covers_the_beat(self):
        short = Candidate("a", 0.0, 0.7)
        long_enough = Candidate("b", 0.0, 3.0)
        chosen, repeats = pick([short, long_enough], need=2.5)
        self.assertIs(long_enough, chosen)
        self.assertFalse(repeats)

    def test_the_stretch_floor_is_the_same_rule_the_matched_path_uses(self):
        """0.84 of the beat may be stretched to fill it; a hair less may not."""
        floor = scrape_v4.V4_STRETCH_FLOOR
        just_enough = Candidate("a", 0.0, 2.5 * floor)
        just_short = Candidate("b", 0.0, 2.5 * floor - 0.2)
        self.assertIs(just_enough, pick([just_short, just_enough], need=2.5)[0],
                      "a window under the stretch floor was preferred over one that covers")
        self.assertIs(just_enough, pick([just_enough], need=2.5)[0])

    def test_a_hole_is_still_worse_than_a_stretched_shot(self):
        """Nothing long enough on the shelf: take the short one rather than leave the beat empty."""
        short = Candidate("a", 0.0, 0.7)
        chosen, _ = pick([short], need=2.5)
        self.assertIs(short, chosen)

    def test_the_beats_own_footage_still_outranks_a_stranger(self):
        mine = Candidate("a", 0.0, 3.0, query="mine")
        stranger = Candidate("b", 0.0, 9.0, query="other")
        chosen, _ = pick([mine, stranger], need=2.5, wanted={"mine"})
        self.assertIs(mine, chosen, "the length test overtook the beat's own query")

    def test_it_outranks_a_stranger_even_when_it_is_too_short(self):
        """The first version ran all three tiers with the length test and then all three again
        without it, so a longer stranger beat the beat's own footage. Length is a tie-break
        inside a tier, never a reason to skip to the next one."""
        mine_short = Candidate("a", 0.0, 1.0, query="mine")
        stranger_long = Candidate("b", 0.0, 9.0, query="other")
        chosen, _ = pick([mine_short, stranger_long], need=2.5, wanted={"mine"})
        self.assertIs(mine_short, chosen,
                      "an unrelated longer window outranked the beat's own footage")

    def test_the_last_resort_tier_prefers_a_covering_window_too(self):
        """7 of the 12 fills of the run this was measured on came from the last resort, which
        had no length test at all - so the rule was missing from the path most fills take."""
        short = Candidate("a", 0.0, 0.8, status="rejected")
        covering = Candidate("b", 0.0, 4.0, status="rejected")
        chosen, needs_replacing = pick([short, covering], need=2.5)
        self.assertIs(covering, chosen)
        self.assertTrue(needs_replacing, "a last-resort pick must still be flagged")

    def test_a_beat_of_unknown_length_still_gets_a_clip(self):
        only = Candidate("a", 0.0, 0.5)
        self.assertIs(only, pick([only], need=0.0)[0])


class TheCallSitePassesTheBeatLength(unittest.TestCase):
    def test_the_fill_loop_measures_the_beat_before_choosing(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scrape_v4.py"), encoding="utf-8").read()
        call = src[src.index("pick, repeats_earlier_shot = _pick_coverage_fill("):][:400]
        self.assertIn("need=_beat_needs", call)
        self.assertIn('_beat_needs = max(0.0, float(out_scenes[index].get("end")', src)


if __name__ == "__main__":
    unittest.main()
