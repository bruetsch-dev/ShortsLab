"""Results that were found - and paid for - must not be discarded unfetched.

On 2026-08-26 a Clip Short batch reached TikTok's daily limit on its first search, fell back to
the paid Bright Data API, and that API returned 30 usable records across three chapters. Every
one was thrown away: the chapter's time budget was already spent, and the download loop broke on
the same deadline that governs searching. The log said "10 raw result(s), 0 source(s) downloaded"
and read like a scraper fault.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DownloadGraceTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "scrape_v3.py").read_text(encoding="utf-8")
        start = self.source.index("    download_grace = None if deadline is None")
        self.loop = self.source[start:start + 1400]

    def test_there_is_a_bounded_grace_for_fetching(self):
        self.assertIn("DOWNLOAD_GRACE_S", self.source)
        self.assertIn("download_grace = None if deadline is None else deadline + DOWNLOAD_GRACE_S",
                      self.loop)

    def test_the_grace_is_bounded_rather_than_unlimited(self):
        """A rescue, not a second search phase."""
        import re
        value = float(re.search(r"^DOWNLOAD_GRACE_S = ([\d.]+)", self.source, re.M).group(1))
        self.assertGreater(value, 0.0)
        self.assertLessEqual(value, 300.0)

    def test_the_deadline_no_longer_ends_the_fetch_on_its_own(self):
        """The old line: `if (cancel_check and cancel_check()) or (deadline and ... >= deadline)`."""
        self.assertNotIn("or (deadline and time.monotonic() >= deadline):", self.loop)

    def test_cancelling_still_stops_immediately(self):
        """A grace period for fetching must never override the user pressing stop."""
        self.assertIn("if cancel_check and cancel_check():\n            break", self.loop)

    def test_the_fetch_stops_once_something_is_in_hand(self):
        self.assertIn("if downloaded or (download_grace and time.monotonic() >= download_grace):",
                      self.loop)

    def test_a_wasted_chapter_says_so(self):
        self.assertIn("were found but none could be fetched", self.source)


class SearchFetchSplitTests(unittest.TestCase):
    """The real fix: searching may not spend the fetch reserve in the first place.

    A chapter gets at most 150 seconds. A single provider call was allowed up to 600. Two calls
    and the clock was gone before one clip had been fetched - so the grace period above was doing
    all the work. The clock is now split before the first query runs.
    """

    def setUp(self):
        self.source = (ROOT / "scrape_v3.py").read_text(encoding="utf-8")

    def test_the_clock_is_split_before_the_first_query(self):
        self.assertIn("SEARCH_SHARE", self.source)
        self.assertIn("search_deadline = (None if deadline is None", self.source)

    def test_the_split_leaves_a_real_fetch_reserve(self):
        import re
        share = float(re.search(r"^SEARCH_SHARE = ([\d.]+)", self.source, re.M).group(1))
        self.assertGreater(share, 0.4, "too little time left to search at all")
        self.assertLess(share, 0.85, "too little time left to fetch what was found")

    def test_searching_honours_the_search_deadline_not_the_whole_budget(self):
        head = self.source[:self.source.index("    for query, item in candidates:")]
        loop = head[head.index("def gather_chapter_sources"):]
        self.assertNotIn("(deadline and time.monotonic() >= deadline)", loop,
                         "a search still measures itself against the full chapter budget")
        # Three sites now: the two search gates plus the network-bound Instagram supplement
        # inside the batched branch. The prefetched-batch READ is deliberately not one of them.
        self.assertEqual(loop.count("search_deadline and time.monotonic() >= search_deadline"), 3)

    def test_the_provider_call_is_bounded_by_the_search_phase(self):
        self.assertIn("deadline=search_deadline) or []", self.source)


class BrightDataSpendTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "clip_scraper.py").read_text(encoding="utf-8")

    def test_a_paid_call_needs_time_to_fetch_its_results(self):
        """Buying records nobody can download charges the bill and discards the goods."""
        self.assertIn("BRIGHTDATA_MIN_REMAINING_S", self.source)
        self.assertIn("if allowance > 0 and bd_remaining >= BRIGHTDATA_MIN_REMAINING_S:",
                      self.source)

    def test_the_threshold_leaves_room_for_the_download(self):
        import re
        value = float(re.search(r"^BRIGHTDATA_MIN_REMAINING_S = ([\d.]+)",
                                self.source, re.M).group(1))
        self.assertGreaterEqual(value, 45.0, "too tight to fetch what was bought")

    def test_one_call_cannot_swallow_a_whole_chapter(self):
        """It was min(600, remaining) inside a 150s chapter.

        This test first asserted a cap of 150s, written before the API had been timed. That was
        wrong and it cost an evening: a live discover job runs 111 seconds, so a 150s cap - and
        the 120s that was actually shipped - killed every call a few seconds before it paid out,
        and the run reported "0 usable record(s)" from an API that was working. The real
        constraint is the SEARCH PHASE of a Bright-Data-bound chapter, not the healthy-mode one.
        """
        import re
        cap = float(re.search(r"^BRIGHTDATA_CALL_TIMEOUT_S = ([\d.]+)",
                              self.source, re.M).group(1))
        v3 = (ROOT / "scrape_v3.py").read_text(encoding="utf-8")
        chapter = float(re.search(r"chapter_cap = ([\d.]+) if tiktok_daily_limited", v3).group(1))
        share = float(re.search(r"^SEARCH_SHARE = ([\d.]+)", v3, re.M).group(1))
        self.assertGreaterEqual(cap, 111.0, "shorter than a measured job: the call is wasted")
        self.assertLessEqual(cap, chapter * share,
                             "one call may still not eat the chapter's whole search phase")
        self.assertIn("timeout_s=min(BRIGHTDATA_CALL_TIMEOUT_S, bd_remaining)", self.source)
        self.assertNotIn("timeout_s=min(600.0, bd_remaining)", self.source)

    def test_the_record_budget_still_caps_the_bill(self):
        self.assertIn("_BRIGHTDATA_RECORDS_USED < BRIGHTDATA_RECORD_BUDGET", self.source)


if __name__ == "__main__":
    unittest.main()


class ApiKeywordWidthTests(unittest.TestCase):
    """Eight keywords buy about three real searches when the provider drops 60% of them.

    Measured across 12 batched jobs on 2026-08-26/27: 30 keywords submitted, 12 came back with
    material. The rest died as `dead_page` without being searched. A chapter cannot be covered
    from three searches, so the API-only net is cast wider; the record budget still caps the bill.
    """

    def setUp(self):
        self.source = (ROOT / "scrape_v3.py").read_text(encoding="utf-8")

    def test_the_api_mode_gets_its_own_width(self):
        self.assertIn('"queries_per_chapter_api"', self.source)
        self.assertIn("def _queries_per_chapter():", self.source)

    def test_a_normal_run_is_left_exactly_as_it_was(self):
        import clip_scraper
        import scrape_v3
        was = clip_scraper.BRIGHTDATA_ONLY
        try:
            clip_scraper.set_brightdata_only(False)
            self.assertEqual(scrape_v3._queries_per_chapter(),
                             scrape_v3.V3_CONFIG["queries_per_chapter"])
        finally:
            clip_scraper.set_brightdata_only(was)

    def test_the_api_mode_is_wider_but_not_unbounded(self):
        import clip_scraper
        import scrape_v3
        was = clip_scraper.BRIGHTDATA_ONLY
        try:
            clip_scraper.set_brightdata_only(True)
            width = scrape_v3._queries_per_chapter()
            self.assertGreater(width, scrape_v3.V3_CONFIG["queries_per_chapter"])
            self.assertLessEqual(width, 24, "a chapter cannot spend the whole record budget")
        finally:
            clip_scraper.set_brightdata_only(was)

    def test_no_query_path_caps_with_the_raw_config_value(self):
        """A cap applied in one place and not the other silently truncates the wider net.

        Counting call sites was the first version of this, and it broke the moment a THIRD path
        was added - which is the case it should have been passing for. Matching every
        sanitize_queries call was the second, and it flagged the deliberate small limits
        (limit=3 for a seed triple, limit=100 for a dedup pass) that have nothing to do with a
        chapter's width. What actually matters is that the widened value is the only chapter
        width in play.
        """
        self.assertNotIn('limit=V3_CONFIG["queries_per_chapter"]', self.source)
        # every chapter-building path asks the function, and there is more than one of them
        chapter_paths = self.source.count("limit=_queries_per_chapter()")
        self.assertGreaterEqual(chapter_paths, 2, "a chapter path stopped using the shared width")

    def test_the_shared_width_is_wider_than_the_raw_config(self):
        """That is the whole point of the function - the config value is the browser-mode
        baseline, and the API mode needs more queries per chapter."""
        import clip_scraper
        import scrape_v3
        was = clip_scraper.BRIGHTDATA_ONLY
        try:
            clip_scraper.set_brightdata_only(True)
            self.assertGreater(scrape_v3._queries_per_chapter(),
                               scrape_v3.V3_CONFIG["queries_per_chapter"])
        finally:
            clip_scraper.set_brightdata_only(was)

    def test_the_record_budget_still_bounds_the_spend(self):
        clip = (ROOT / "brightdata_tiktok.py").read_text(encoding="utf-8")
        self.assertIn("wanted = wanted[:max(1, room // per_query)]", clip)


class BatchReadNotGatedTests(unittest.TestCase):
    """Reading results already in hand must not be gated by the search clock.

    The batched keyword job spends 60-110s of the same clock that gates collect() - so the job
    finished, 50 paid records sat in memory, and every collect() returned "out of time" without
    looking at them. konbini_night: "50 record(s) across 6 keyword(s)" followed immediately by
    "0 raw result(s), 16 search(es) never ran - the scrape time budget was already spent".
    """

    def setUp(self):
        self.source = (ROOT / "scrape_v3.py").read_text(encoding="utf-8")

    def test_the_deadline_gate_exempts_the_prefetched_batch(self):
        self.assertIn("and api_results is None:", self.source)

    def test_cancelling_still_stops_collect_immediately(self):
        block = self.source[self.source.index("def collect(query, sort_mode):"):]
        self.assertIn("if cancel_check and cancel_check():\n            skipped_out_of_time += 1"
                      "\n            return", block[:900])

    def test_the_network_bound_instagram_supplement_keeps_the_clock(self):
        self.assertIn("and not (search_deadline and time.monotonic() >= search_deadline)",
                      self.source)
