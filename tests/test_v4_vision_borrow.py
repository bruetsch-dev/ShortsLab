"""A beat with no query match of its own may borrow a clip that vision saw doing the thing.

Measured on a live V4 run 2026-09-02 (`restocking_japan_s_hot_winter_vending_machines_is`):
7 of 12 beats were filled and the run refused to render, while 16 distinct clips sat accepted
and unused. Several were at relevance 10, including "Real moving footage showing a worker
restocking a Japanese vending machine" - which could not fill the restocking beat, because a
different beat's query had been the one to retrieve it.

Borrowing used to be banned outright, and for a good reason: when the only evidence was the
post's own caption, borrowing turned a romantic travel montage into luggage delivery. The vision
verdict describes the footage itself, so the ban is now conditional on that description.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v4


RESTOCK_BEAT = {
    "id": "b5", "start": 0.0, "end": 3.0,
    "text": "Route drivers race across cities restocking vending machines by hand.",
    "visual_subject": "route driver", "visual_action": "restocking a vending machine",
}


def borrowable(verdict_reason, relevance=10, accept=True):
    """The exact test the assignment stage applies to a clip from another beat's query."""
    if not accept or float(relevance) < 8:
        return False
    hits, unrelated = scrape_v4._metadata_relevance(
        RESTOCK_BEAT, "日本 自販機 青ラベル 赤ボタン", {"desc": verdict_reason})
    return bool(hits) and not unrelated


class VisionBorrowing(unittest.TestCase):

    def test_the_clip_that_shows_the_beat_can_be_borrowed(self):
        self.assertTrue(borrowable(
            "Real moving footage showing a worker restocking a Japanese vending machine."))

    def test_an_unrelated_clip_cannot(self):
        self.assertFalse(borrowable("A cat sleeping on a sofa in a living room."))

    def test_a_weak_verdict_cannot_be_borrowed_however_well_it_reads(self):
        """Borrowing is for strong verdicts only; 7/10 stays with the beat that searched it."""
        self.assertFalse(borrowable(
            "Real moving footage showing a worker restocking a Japanese vending machine.",
            relevance=7))

    def test_a_rejected_clip_cannot_be_borrowed(self):
        self.assertFalse(borrowable(
            "Real moving footage showing a worker restocking a Japanese vending machine.",
            accept=False))


class BorrowingIsVisibleAndConditional(unittest.TestCase):
    """The behaviour lives inline in a long function; pin the parts a reader would check."""

    def setUp(self):
        with open(scrape_v4.__file__, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_borrowing_only_runs_when_the_beat_has_nothing(self):
        self.assertIn("if not _has_usable_line_evidence(scene, matched):", self.source)
        self.assertIn("matched = matched + borrowed", self.source)

    def test_a_borrowed_clip_is_labelled_as_such(self):
        self.assertIn('"vision_matched" if chosen.borrowed_from else "query_matched"',
                      self.source)


if __name__ == "__main__":
    unittest.main()
