"""A sentence written BEFORE the work is not a record of the work.

Two artifacts of the 4h07m V4 run described a state that had long since changed, and both were
read by a reviewer as evidence about the run:

    v4_reason = "native vertical moving footage; awaiting V4 vision review"
        on all fifteen assigned scenes - including the three whose windows the vision pass had
        actually verified. It is the sentence the window carried before the review, and nothing
        overwrote it afterwards.

    review/live_processing_timeline.json
        written only by scrape_v3 (which deletes it when its own run starts), never by
        scrape_v4. A project scraped by V3 and re-scraped by V4 keeps the older file, saying
        `engine: scrape_v3` with the older timestamps, and it reads exactly like this run's.

Neither is a lie the code tells on purpose; both are what happens when a record is written once
and never revised. Stale evidence is worse than none: no evidence makes a reader go and look.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v3
import scrape_v4


class TheVerdictReplacesThePlaceholder(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(scrape_v4._vision_source_review)

    def test_the_reviewed_window_stops_saying_it_is_awaiting_review(self):
        self.assertIn('candidate.reason.split("; awaiting V4 vision review")[0]', self.src)

    def test_and_it_says_what_the_reviewer_saw(self):
        self.assertIn('decision.get("observed_action")', self.src)
        self.assertIn("; reviewed", self.src)

    def test_every_way_of_writing_it_ends_with_the_same_suffix(self):
        """The overwrite strips one exact suffix, so every branch must end in it.

        This scanned the whole module through a `hasattr` fallback on a function that does not
        exist, so it passed while testing nothing. The sentence is written inside
        `inspect_record`, the nested window enumerator - read that, and fail if it moves.
        """
        whole = inspect.getsource(scrape_v4)
        self.assertIn("        def inspect_record(", whole,
                      "inspect_record has been renamed; this test no longer reads the code "
                      "that writes the placeholder")
        start = whole.index("        def inspect_record(")
        body = whole[start:whole.index("\n        def ", start + 10)] \
            if "\n        def " in whole[start + 10:] else whole[start:]
        written = [line for line in body.splitlines()
                   if "awaiting V4 vision review" in line and not line.strip().startswith("#")
                   and "candidate.reason" not in line]
        self.assertTrue(written, "the pre-review sentence is not written anywhere any more")
        for line in written:
            self.assertIn("awaiting V4 vision review\"", line,
                          f"this branch ends the sentence differently, so the overwrite at "
                          f"_vision_source_review will leave it in place: {line.strip()}")


class NoEngineInheritsAnothersTimeline(unittest.TestCase):
    def test_v4_clears_the_file_it_does_not_write(self):
        src = inspect.getsource(scrape_v4.scrape_social_plan_v4)
        self.assertIn('(review / "live_processing_timeline.json").unlink(missing_ok=True)', src)

    def test_v3_still_clears_its_own(self):
        whole = inspect.getsource(scrape_v3)
        self.assertIn('"live_processing_timeline.json").unlink(missing_ok=True)', whole)


if __name__ == "__main__":
    unittest.main()
