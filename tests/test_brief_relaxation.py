"""A beat with no footage is the worst outcome the scraper can produce.

Measured on the delivered Tokyo-train Clip Short (2026-08-29): chapter 3 downloaded 20 sources
and accepted NONE. Its brief asked for four things at once - "One continuous sequence must show
the doors opening, the friends leaving the train, turning toward one another, and visibly
talking or gesturing together on the platform". That is a scripted narrative shot; nobody posts
one, so the vision gate was right and the brief was wrong.

Proof, not inference: the same 20 frame strips were re-graded with the same vision model
(gemini 3.1 flash lite) against "a station platform with passengers who have left, or are
leaving, a train". Accepted 1/20 under the original brief, 3/20 under the single condition.
"""

import unittest

import scrape_v3 as v3


COMPOUND = ("One continuous sequence must show the doors opening, the friends leaving the train, "
            "turning toward one another, and visibly talking or gesturing together on the platform.")


class DetectionTests(unittest.TestCase):
    def test_the_measured_brief_is_recognised_as_compound(self):
        self.assertTrue(v3._is_compound_evidence(COMPOUND))

    def test_a_single_condition_is_left_alone(self):
        for brief in ("a station platform with passengers leaving a train",
                      "a capsule hotel pod with the curtain pulled down",
                      "a train carriage poster asking for quiet"):
            self.assertFalse(v3._is_compound_evidence(brief), brief)

    def test_the_chaining_words_are_each_detected(self):
        """Each branch of the pattern must actually fire. These read as one condition by word
        count, so only the phrase itself can catch them - and a corrupted pattern would let
        them through in silence."""
        for brief in ("a poster asking for quiet, followed by passengers sitting",
                      "a capsule hotel pod and then the curtain",
                      "one continuous shot of a platform"):
            self.assertTrue(v3._is_compound_evidence(brief), brief)

    def test_an_empty_brief_is_not_compound(self):
        self.assertFalse(v3._is_compound_evidence(""))
        self.assertFalse(v3._is_compound_evidence(None))


class SimplifyTests(unittest.TestCase):
    def test_the_sequence_preamble_is_removed(self):
        got = v3.simplify_evidence(COMPOUND)
        self.assertNotIn("continuous", got.lower())
        self.assertNotIn("must show", got.lower())

    def test_only_the_first_condition_survives(self):
        got = v3.simplify_evidence(COMPOUND)
        self.assertNotIn("gesturing", got.lower())
        self.assertIn("doors opening", got.lower())

    def test_a_very_short_first_clause_keeps_the_next_one(self):
        """'the doors opening' alone names a moment, not a searchable subject."""
        got = v3.simplify_evidence(COMPOUND)
        self.assertGreaterEqual(len(got.split()), 4, got)

    def test_it_never_returns_nothing(self):
        for brief in (COMPOUND, "a single thing", "", "one continuous shot must show a train"):
            self.assertIsInstance(v3.simplify_evidence(brief), str)


class WiringTests(unittest.TestCase):
    def test_an_empty_chapter_triggers_the_rescue(self):
        source = open(v3.__file__, encoding="utf-8").read()
        self.assertIn("rescue_empty_chapter(", source)
        block = source[source.index("for chapter in chapters:\n        if windows_by_chapter.get"):]
        block = block[:block.index("drop_cross_chapter_reuse")]
        self.assertIn("rescue_empty_chapter", block)

    def test_the_rescue_spends_no_new_downloads(self):
        """It re-judges footage already paid for; a second search would cost another budget."""
        source = open(v3.__file__, encoding="utf-8").read()
        body = source[source.index("def rescue_empty_chapter("):]
        body = body[:body.index("\ndef ", 10)]
        for forbidden in ("gather_chapter_sources(", "search_platform(",
                          "download_source(", "chapter_search_queries(",
                          "adaptive_search_queries("):
            self.assertNotIn(forbidden, body, forbidden)
        self.assertIn("grade_sources_v3(", body,
                      "the rescue must still LOOK at the footage")

    def test_a_rescue_failure_never_aborts_the_run(self):
        source = open(v3.__file__, encoding="utf-8").read()
        body = source[source.index("def rescue_empty_chapter("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("except Exception", body)

    def test_the_planner_is_told_to_write_one_condition(self):
        """The rescue is a net. Preventing the compound brief is the actual fix."""
        self.assertIn("ONE observable condition", v3.CHAPTER_PROMPT)


if __name__ == "__main__":
    unittest.main()
