"""A clip must be worth watching, not merely correct.

Reported 2026-08-30: "die meisten sind low quality clips und uninteressant. ich sagte doch mal
es sollen eher weird clips sein."

The cause was in the ranking, not in the search. The whole order was:

    flat.sort(key=lambda w: (rank.get(w.match_class, 3), -float(w.duration)))

so among windows of the same match class the LONGEST won - and that tiebreaker actively rewards
dead footage: a locked-off, uneventful shot yields one long usable window, while a lively clip
is chopped into several short ones. The boring take won every tie. Nothing in the vision pass
asked whether a moment was watchable at all; `match_class` only says whether it fits.
"""

import unittest

import scrape_v3 as v3


def window(interest, duration, match_class="exact", sid=None):
    return v3.ShotWindow(chapter_id=1, source_id=sid or f"s{interest}_{duration}",
                         platform="tiktok", path="/tmp/x.mp4",
                         start=0.0, end=float(duration),
                         match_class=match_class, visual_interest=interest)


RANK = {"exact": 0, "context": 1, "editorial_proxy": 2}


def order(windows):
    out = list(windows)
    out.sort(key=lambda w: (RANK.get(w.match_class, 3),
                            -v3._interest_band(w.visual_interest),
                            -float(w.duration)))
    return out


class ParseTests(unittest.TestCase):
    def test_the_score_is_read_from_the_vision_reply(self):
        self.assertEqual(v3._interest_of({"visual_interest": 9}), 9.0)
        self.assertEqual(v3._interest_of({"visual_interest": "7.5"}), 7.5)

    def test_it_is_clamped(self):
        self.assertEqual(v3._interest_of({"visual_interest": 99}), 10.0)
        self.assertEqual(v3._interest_of({"visual_interest": -3}), 0.0)

    def test_a_missing_score_is_neutral_not_zero(self):
        """A malformed reply must not push good footage below everything scored."""
        self.assertEqual(v3._interest_of({}), 5.0)
        self.assertEqual(v3._interest_of({"visual_interest": "nonsense"}), 5.0)

    def test_a_window_defaults_to_neutral(self):
        self.assertEqual(window(5, 1).visual_interest, 5.0)


class BandTests(unittest.TestCase):
    def test_the_bands_match_the_prompt(self):
        self.assertEqual(v3._interest_band(9), 2)      # stop-scrolling
        self.assertEqual(v3._interest_band(6), 1)      # ordinary
        self.assertEqual(v3._interest_band(3), 0)      # inert

    def test_banding_ignores_noise_between_neighbours(self):
        """The vision pass cannot tell 7.0 from 7.4; length should decide inside a band."""
        self.assertEqual(v3._interest_band(7.0), v3._interest_band(7.4))


class RankingTests(unittest.TestCase):
    def test_a_striking_short_shot_beats_a_dead_long_one(self):
        """The exact inversion that was reported."""
        got = order([window(3, 8.0), window(9, 2.0)])
        self.assertEqual(got[0].visual_interest, 9)

    def test_length_still_decides_inside_a_band(self):
        got = order([window(6, 2.0), window(7, 5.0)])
        self.assertEqual(got[0].duration, 5.0)

    def test_fit_still_outranks_interest(self):
        """A thrilling shot of the wrong thing is still the wrong thing."""
        got = order([window(10, 3.0, match_class="context"),
                     window(3, 3.0, match_class="exact")])
        self.assertEqual(got[0].match_class, "exact")

    def test_inert_footage_is_last_but_not_discarded(self):
        """A hole is worse than a dull shot - it may still fill a beat nothing else covers."""
        got = order([window(2, 9.0), window(6, 1.0)])
        self.assertEqual(got[-1].visual_interest, 2)
        self.assertEqual(len(got), 2)


class PromptTests(unittest.TestCase):
    def test_the_grader_is_asked_for_the_score(self):
        self.assertIn("visual_interest", v3.GRADE_PROMPT)
        self.assertIn("RATE HOW WATCHABLE", v3.GRADE_PROMPT)

    def test_it_is_asked_separately_from_fit(self):
        """Otherwise it just restates match_class and ranks nothing."""
        self.assertIn("never raise the score because the clip fits", v3.GRADE_PROMPT)

    def test_the_report_records_it(self):
        source = open(v3.__file__, encoding="utf-8").read()
        block = source[source.index('row["accepted"]'):]
        block = block[:block.index("for item in graded]")]
        self.assertIn('"visual_interest"', block)


if __name__ == "__main__":
    unittest.main()


class EndToEndCarryTests(unittest.TestCase):
    """The score has to survive every hand-built dict between the model and the ranking.

    Measured on a whole live run (2026-08-30): the prompt asked for it, the vision model
    answered with it, the report printed a column for it - and all 11 windows still reached the
    ranking as the neutral default 5.0, because `grade_sources_v3` rebuilds each window field by
    field and simply had no line for the new key. The feature was inert end to end while every
    unit test passed, because the tests built ShotWindow directly.
    """

    def test_the_grader_copies_the_score_into_its_window_dict(self):
        source = open(v3.__file__, encoding="utf-8").read()
        body = source[source.index("def grade_sources_v3("):]
        body = body[:body.index("\ndef ", 10)]
        block = body[body.index('windows.append({"start"'):]
        block = block[:block.index("})") + 2]
        self.assertIn("visual_interest", block,
                      "the rebuilt window dict drops the score before it can rank anything")

    def test_the_selector_reads_it_back(self):
        source = open(v3.__file__, encoding="utf-8").read()
        body = source[source.index("def select_chapter_windows("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("visual_interest=_interest_of(window)", body)

    def test_a_graded_window_dict_round_trips(self):
        """The shape the grader produces must parse back to the same number."""
        graded = {"start": 0.0, "end": 3.0, "match_class": "exact",
                  "visual_interest": _RAW_SCORE, "reason": "r", "visual_signature": "s"}
        self.assertEqual(v3._interest_of(graded), _RAW_SCORE)


_RAW_SCORE = 9.0
