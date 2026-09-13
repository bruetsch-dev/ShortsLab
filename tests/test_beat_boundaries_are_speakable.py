"""A beat ends where the picture cuts, so a beat must not end on a word that wants the next one.

Measured on the eating-walk Short. The cut times were exact - each one landed within 4 ms of the
last word of its beat, so the timing machinery was doing its job perfectly. What was wrong was
the beats themselves:

    "Japanese people almost never"                    -> cut after an adverb
    "at a crowded night market, you must"             -> cut after a modal
    "stop and stand completely still beside"          -> cut after a preposition
    "Taking even three steps while chewing gets you"  -> cut after a pronoun
    "judged instantly, because dropping crumbs or"    -> cut after a conjunction
    "spilling sauce on the pavement is seen as selfishly"

Six of fourteen picture changes landed on a word that demands continuation, which is what "the
cuts are not on the voiceover" actually describes - not a timing error, a text error.

The repair moves the boundary rather than dissolving the beat: merging each dangling beat into
the next also fixes the grammar, but it collapsed fourteen beats into nine with one running
twenty words. Short-form lives on that cut rhythm, so the trailing words move to the next beat
instead and the count is kept.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core

# verbatim from projects/japanese_people_almost_never_eat_food_while
SHIPPED = [
    "Japanese people almost never",
    "eat food while walking down the street.",
    "When you buy a piping hot skewer",
    "at a crowded night market, you must",
    "stop and stand completely still beside",
    "the stall until you finish the last bite.",
    "Taking even three steps while chewing gets you",
    "judged instantly, because dropping crumbs or",
    "spilling sauce on the pavement is seen as selfishly",
    "dirtying shared public space.",
    "Because Japan has almost zero public trash cans,",
    "customers quietly hand empty wooden sticks",
    "and greasy wrappers right back",
    "to the shopkeeper before walking away.",
]


class TheDetectorKnowsADanglingTail(unittest.TestCase):
    def test_it_catches_the_six_that_shipped(self):
        caught = [b for b in SHIPPED if agent_core.beat_tail_dangles(b)]
        self.assertEqual(len(caught), 6, caught)

    def test_a_finished_sentence_never_dangles(self):
        for good in ("eat food while walking down the street.",
                     "dirtying shared public space.",
                     "Is that so?", "Stop!"):
            self.assertFalse(agent_core.beat_tail_dangles(good), good)

    def test_articles_prepositions_conjunctions_modals_and_pronouns_all_count(self):
        for bad in ("he reached for the", "still standing beside", "crumbs or",
                    "you must", "at a crowded night market, you", "almost"):
            self.assertTrue(agent_core.beat_tail_dangles(bad), bad)

    def test_a_mid_sentence_adverb_counts(self):
        self.assertTrue(agent_core.beat_tail_dangles("is seen as selfishly"))

    def test_empty_and_junk_do_not_crash(self):
        for junk in ("", "   ", None, "...", "42"):
            agent_core.beat_tail_dangles(junk)


class TheRepairMovesTheBoundary(unittest.TestCase):
    def setUp(self):
        self.fixed = agent_core.repair_beat_boundaries(SHIPPED)

    def test_nothing_dangles_afterwards(self):
        left = [b for b in self.fixed if agent_core.beat_tail_dangles(b)]
        self.assertEqual(left, [])

    def test_the_cut_rhythm_survives(self):
        """Merging would have fixed the grammar and destroyed the edit: 14 beats to 9, one of
        them twenty words. Shifting keeps the count within one."""
        self.assertGreaterEqual(len(self.fixed), len(SHIPPED) - 1)
        longest = max(len(re.findall(r"\w+", b)) for b in self.fixed)
        self.assertLessEqual(longest, 14, self.fixed)

    def test_not_one_spoken_word_is_lost_or_invented(self):
        before = re.findall(r"[\w']+", " ".join(SHIPPED).lower())
        after = re.findall(r"[\w']+", " ".join(self.fixed).lower())
        self.assertEqual(before, after)

    def test_clean_input_is_returned_untouched(self):
        clean = ["The rule is simple.", "Nobody eats on the street.", "Everybody knows it."]
        self.assertEqual(agent_core.repair_beat_boundaries(clean), clean)

    def test_it_survives_a_single_beat_and_an_empty_list(self):
        self.assertEqual(agent_core.repair_beat_boundaries([]), [])
        self.assertEqual(agent_core.repair_beat_boundaries(["you must"]), ["you must"])


class BothProducersUseIt(unittest.TestCase):
    def test_the_deterministic_splitter_repairs_its_output(self):
        src = inspect_source(agent_core.split_micro_units)
        self.assertIn("repair_beat_boundaries(merged)", src)

    def test_the_llm_plan_is_repaired_before_anything_is_built_from_it(self):
        src = inspect_source(agent_core.normalize_micro_beat_plan)
        self.assertIn("repair_beat_boundaries(_texts)", src)
        self.assertIn("THE MODEL IS NOT ASKED WHERE THE PICTURE MAY CUT", src)


def inspect_source(fn):
    import inspect as _i
    return _i.getsource(fn)


if __name__ == "__main__":
    unittest.main()
