"""A chapter may not call itself covered while resting on one or two uploads.

The delivered run `japan_has_hotels_where_you_can_stay` is the whole argument. Its report:

    chapter 0: 8 downloaded,  1 survived   rounds run: 0
    chapter 1: 22 downloaded, 3 survived   rounds run: 0
    chapter 2: 18 downloaded, 3 survived   rounds run: 0
    chapter 3: 22 downloaded, 2 survived   rounds run: 0

Every chapter finished far below `min_sources_per_chapter` (8) and NONE of them searched again.
The reason is that `chapter_search_satisfied` counted WINDOWS: three surviving uploads sliced
into six windows covered six beats, so the chapter declared itself done and the search stopped.
The finished Short then showed nine scenes cut from six videos, one TikTok carrying three of
them - which is what the user saw as "3 verschiedene clips, nur duplikate".

Requiring distinct SOURCES turns that into another search round instead of a loop.
"""

import unittest

import scrape_v3 as v3


class Window:
    def __init__(self, source_id, duration, match_class="exact"):
        self.source_id = source_id
        self.duration = duration
        self.match_class = match_class


def chapter(beats, span):
    return v3.Chapter(chapter_id=1, title="t", scene_ids=["s"] * beats, start=0.0, end=span,
                      evidence="")


def windows(sources, beats, span):
    """`beats` windows spread round-robin over `sources` distinct uploads."""
    return [Window("src%d" % (i % sources), span / max(1, beats)) for i in range(beats)]


class SatisfactionTests(unittest.TestCase):
    def test_the_chapter_that_produced_the_duplicates_is_no_longer_satisfied(self):
        """Chapter 3 of the real run: five beats resting on two uploads."""
        self.assertFalse(v3.chapter_search_satisfied(chapter(5, 10.69), windows(2, 5, 10.69)))

    def test_three_uploads_across_six_beats_is_accepted(self):
        """Not a demand for one source per beat - that would fail every honest run."""
        self.assertTrue(v3.chapter_search_satisfied(chapter(6, 11.75), windows(3, 6, 11.75)))

    def test_a_single_beat_chapter_still_needs_only_one_source(self):
        """The hook is one beat; demanding three uploads for it would never finish."""
        self.assertTrue(v3.chapter_search_satisfied(chapter(1, 1.6), windows(1, 1, 1.6)))

    def test_two_beats_need_only_two_sources(self):
        """The requirement is capped by the number of beats."""
        self.assertTrue(v3.chapter_search_satisfied(chapter(2, 4.65), windows(2, 2, 4.65)))

    def test_one_upload_sliced_into_many_windows_is_refused(self):
        """The exact mechanism that ended the search early."""
        self.assertFalse(v3.chapter_search_satisfied(chapter(6, 11.75), windows(1, 6, 11.75)))


class ConfigTests(unittest.TestCase):
    def test_the_variety_floor_exists_and_is_modest(self):
        floor = v3.V3_CONFIG["min_distinct_sources"]
        self.assertGreaterEqual(floor, 2)
        self.assertLessEqual(floor, 4, "a high floor fails honest runs on rare subjects")

    def test_a_source_cannot_carry_more_shots_than_the_whole_short_budget(self):
        self.assertEqual(v3.V3_CONFIG["max_windows_per_source"],
                         v3.V3_CONFIG["max_windows_per_source_total"])

    def test_the_check_looks_at_sources_not_only_windows(self):
        source = open(v3.__file__, encoding="utf-8").read()
        body = source[source.index("def chapter_search_satisfied("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("distinct_sources", body)
        self.assertIn("min_distinct_sources", body)


if __name__ == "__main__":
    unittest.main()
