"""The edit map must spend beats according to where the footage comes from.

Measured on the mirrors Fact Short (2026-09-03): a 30-second script became 24 narration beats and
14 chapters. The run found 7 distinct usable sources, so 8 of the 14 shots were filled with
footage found for a different sentence, and the video read as unrelated clips over a voiceover.

The old prompt asked for exactly that. It told the model "a reference Short that holds attention
runs a 1.14s MEDIAN shot and 13 shots in 20 seconds" and "do not let a long sentence become one
long beat". That is right when the renderer can produce any shot the plan names, and wrong when
every beat is a separate video a stranger had to have filmed and uploaded - roughly one candidate
in ten survives the review.

Measured after the change, same script and model (98 words, ~30s):

    generate branch   17 beats, median 1.80s
    scrape branch      8 beats, median 3.81s
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core


def planner_source():
    return inspect.getsource(agent_core.llm_micro_beat_plan)


class ThePlannerKnowsWhereFootageComesFrom(unittest.TestCase):

    def test_both_entry_points_take_the_footage_source(self):
        for fn in (agent_core.llm_micro_beat_plan, agent_core.build_micro_beat_plan):
            with self.subTest(fn=fn.__name__):
                self.assertIn("footage_source", inspect.signature(fn).parameters)

    def test_the_run_passes_the_projects_clip_source(self):
        with open(agent_core.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('footage_source=str(form.get("clip_source") or "generate").strip().lower()',
                      source)

    def test_scrape_and_generate_get_different_pacing(self):
        body = planner_source()
        self.assertIn('scraping = str(footage_source or "generate").strip().lower() in ("scrape", "scraped", "social")',
                      body)
        self.assertIn("HOW MANY BEATS YOU MAY SPEND", body)


class TheScrapeBudgetIsExplicit(unittest.TestCase):

    def setUp(self):
        self.body = planner_source()

    def test_it_states_what_a_beat_actually_costs(self):
        self.assertIn("a separate real video that a stranger filmed and posted", self.body)
        self.assertIn("rejects", self.body)

    def test_it_names_the_average_rather_than_a_ceiling(self):
        """Superseded 2026-09-04. "A shot every 2.5 to 4 seconds" and "one person doing one thing
        is ONE beat" were written while discovery was starving, to cut the number of clips a run
        had to find. They worked: five finished Shorts came back with 4-5 shots each, medians of
        3.7-5.1s and single shots of 6.3, 7.0 and 7.9s, every long one part frozen. Supply is no
        longer the constraint - the same runs return 86-117 usable windows - so the rule is now
        the user's own average, 2.3s, and length is a retention question rather than a budget."""
        self.assertIn("2.55 SECONDS IS THE AVERAGE", self.body)
        self.assertIn("roughly 12 to 14 beats", self.body)
        self.assertNotIn("a shot every 2.5 to 4 seconds", self.body)

    def test_a_long_sentence_is_split_rather_than_held(self):
        self.assertIn("the hands instead of the room", self.body)
        self.assertIn("Two shots of one action are two beats", self.body)

    def test_an_unfilmable_line_does_not_get_its_own_beat(self):
        self.assertIn("BEFORE YOU WRITE A BEAT, NAME THE VIDEO IT NEEDS", self.body)
        self.assertIn("must be merged into its neighbour", self.body)


class TheGeneratedPathKeepsItsFastCadence(unittest.TestCase):
    """A generated run can render any shot the plan names, so nothing there is scarce."""

    def test_the_reference_cadence_still_exists_for_generated_media(self):
        body = planner_source()
        self.assertIn("1.14s MEDIAN shot and 13 shots in 20 seconds", body)
        # ...but only in the branch that is not scraping
        scrape_at = body.index("HOW MANY BEATS YOU MAY SPEND")
        else_at = body.index("else:", scrape_at)
        reference_at = body.index("1.14s MEDIAN shot")
        self.assertGreater(reference_at, else_at,
                           "the fast reference cadence leaked into the scrape branch")


if __name__ == "__main__":
    unittest.main()
