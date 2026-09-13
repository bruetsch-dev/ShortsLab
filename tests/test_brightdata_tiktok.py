"""The paid TikTok fallback, checked against the shape the API actually returns.

Every field here was verified against one real Bright Data record. The mapping had been written
against a guessed schema, so the fallback had never produced a single usable item: it read an
"author" object the payload does not contain, and it called int() on a create_time that arrives
as an ISO-8601 string - which raised inside an unguarded loop and took the whole paid batch with
it.
"""

import unittest

import brightdata_tiktok as bd
import clip_scraper
import tiktok_login

# Trimmed from a real response for "solo karaoke booth Japan".
RECORD = {
    "post_id": "7648206070275476768",
    "url": "https://www.tiktok.com/@xlauramoreira/video/7648206070275476768",
    "description": "God bless Japanese Karaoke, solo booth in Japan",
    "account_id": "xlauramoreira",
    "profile_username": "Laura Moreira",
    "profile_url": "https://www.tiktok.com/@xlauramoreira",
    "create_time": "2026-06-06T09:05:47.000Z",
    "timestamp": "2026-08-26T00:13:41.086Z",
    "digg_count": 24,
    "play_count": 844,
    "width": 576,
    "video_duration": 38,
    "hashtags": ["thingstodointokyo", "japan", "japantiktok"],
}


class NormaliseTests(unittest.TestCase):
    def test_an_iso_timestamp_no_longer_raises(self):
        item = bd._normalise(dict(RECORD))
        self.assertIsNotNone(item)
        self.assertEqual(item["createTime"], 1780736747)

    def test_a_unix_timestamp_still_works(self):
        record = dict(RECORD, create_time=1780736747)
        self.assertEqual(bd._normalise(record)["createTime"], 1780736747)

    def test_an_unreadable_timestamp_degrades_to_zero(self):
        for bad in ({"not": "a time"}, "", "yesterday", None):
            self.assertEqual(bd._epoch(bad), 0)

    def test_the_author_comes_from_the_fields_that_exist(self):
        """The payload has no author object; the handle is account_id and the display name is
        profile_username. Reading the old keys left every item with an empty author, which the
        matcher uses for attribution and de-duplication."""
        item = bd._normalise(dict(RECORD))
        self.assertEqual(item["author"]["uniqueId"], "xlauramoreira")
        self.assertEqual(item["author"]["nickname"], "Laura Moreira")

    def test_the_handle_falls_back_to_the_profile_url(self):
        record = dict(RECORD)
        record.pop("account_id")
        self.assertEqual(bd._normalise(record)["author"]["uniqueId"], "xlauramoreira")

    def test_the_item_reads_correctly_through_the_scraper_metadata(self):
        meta = clip_scraper._item_meta(bd._normalise(dict(RECORD)))
        self.assertEqual(meta["author"], "xlauramoreira")
        self.assertAlmostEqual(meta["duration"], 38.0, places=1)


class BatchTests(unittest.TestCase):
    def test_one_bad_record_costs_one_record_not_the_batch(self):
        class Poison(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("malformed record")

        logs = []
        original = bd._request
        bd._request = lambda url, data=None, timeout=90: (200, [dict(RECORD), Poison(),
                                                               dict(RECORD)])
        try:
            items = bd.search("solo karaoke booth Japan", limit=5, status_cb=logs.append,
                              timeout_s=30)
        finally:
            bd._request = original
        self.assertEqual(len(items), 2)
        self.assertTrue(any("skipped 1 record" in line for line in logs),
                        f"the skip was not reported: {logs}")


class SortingTests(unittest.TestCase):
    def test_api_results_can_be_scored_by_the_browser_path_scorer(self):
        """Bright Data cannot order results server-side, so the sort the user picks has to be
        applied locally - and it keys on _query_relevance. Without a score these items fell back
        to a hardcoded 0.5 and the chosen sort meant nothing for them."""
        item = bd._normalise(dict(RECORD))
        strong = tiktok_login._query_relevance(item, "solo karaoke booth Japan")
        weak = tiktok_login._query_relevance(item, "downhill mountain biking crash")
        self.assertGreater(float(strong), float(weak))
        self.assertGreaterEqual(float(strong), 0.9)

    def test_the_fallback_scores_what_it_receives(self):
        from pathlib import Path
        source = Path(clip_scraper.__file__).read_text(encoding="utf-8")
        block = source[source.index("bd_items = brightdata_tiktok.search"):]
        block = block[:block.index("if ig_future is not None")]
        self.assertIn('candidate["_query_relevance"]', block,
                      "API results are being merged without a relevance score again")


class V3SortModeTests(unittest.TestCase):
    """Scrape V3 referenced V3_SORT_MODES in both of its discovery loops but never defined it,
    so every V3 run died with NameError on its very first search - the engine could not scrape
    at all. The names have to be ones clip_scraper/tiktok_login already understand."""

    def test_the_sort_modes_exist_and_start_with_relevance(self):
        import scrape_v3
        self.assertTrue(scrape_v3.V3_SORT_MODES)
        self.assertEqual(scrape_v3.V3_SORT_MODES[0], "RELEVANCE")

    def test_every_mode_is_one_the_search_backends_understand(self):
        import scrape_v3
        known = {"RELEVANCE", "MOST_LIKED", "MOST_VIEWED", "MOST_RECENT"}
        self.assertTrue(set(scrape_v3.V3_SORT_MODES) <= known,
                        f"unknown sort mode: {set(scrape_v3.V3_SORT_MODES) - known}")

    def test_the_widening_pass_has_something_left_to_widen_with(self):
        """The second pass iterates everything except RELEVANCE; a one-entry tuple would make it
        a silent no-op and the engine would never broaden a thin chapter."""
        import scrape_v3
        self.assertTrue([m for m in scrape_v3.V3_SORT_MODES if m != "RELEVANCE"])

    def test_an_api_result_sorts_on_the_same_scale_as_a_browser_result(self):
        """The paid fallback cannot order server-side, so these sorts run locally on the score
        and on the item's own like/view/date metadata - which is why the mapping had to be right."""
        item = bd._normalise(dict(RECORD))
        item["_query_relevance"] = float(
            tiktok_login._query_relevance(item, "solo karaoke booth Japan"))
        meta = clip_scraper._item_meta(item)
        self.assertGreater(item["_query_relevance"], 0.5, "still on the fabricated default")
        self.assertEqual(int(meta.get("likes") or 0), 24)
        self.assertGreater(int(item["createTime"]), 0)


if __name__ == "__main__":
    unittest.main()


class ChapterGroundingTests(unittest.TestCase):
    """A finished short rested a whole chapter on ONE loosely-related clip. The grading prompt
    deliberately encourages "context" matches for invisible concepts, so they are plentiful and
    cheap - accepting a single one as proof is what let weak footage into the edit."""

    def _chapter(self):
        import scrape_v3
        return scrape_v3.Chapter(chapter_id=0, title="t", scene_ids=["0"], start=0.0, end=8.0,
                                 subject="s", action="a", evidence="e", proxies_ok=True,
                                 forbidden=[], queries=[], allow_multi_window=True, is_hook=False)

    def _window(self, match_class, source_id, seconds=6.0):
        import scrape_v3
        return scrape_v3.ShotWindow(chapter_id=0, source_id=source_id, platform="tiktok",
                                    path="p.mp4", start=0.0, end=seconds,
                                    match_class=match_class)

    def test_one_exact_match_proves_a_chapter(self):
        import scrape_v3
        self.assertTrue(scrape_v3.chapter_search_satisfied(
            self._chapter(), [self._window("exact", "a"), self._window("context", "b")]))

    def test_a_single_context_clip_no_longer_carries_a_chapter(self):
        import scrape_v3
        self.assertFalse(scrape_v3.chapter_search_satisfied(
            self._chapter(), [self._window("context", "a")]))

    def test_a_clip_cannot_corroborate_itself(self):
        """Two context windows cut out of the SAME source are one piece of evidence."""
        import scrape_v3
        self.assertFalse(scrape_v3.chapter_search_satisfied(
            self._chapter(), [self._window("context", "a"), self._window("context", "a")]))

    def test_two_independent_context_sources_are_enough(self):
        import scrape_v3
        self.assertTrue(scrape_v3.chapter_search_satisfied(
            self._chapter(), [self._window("context", "a"), self._window("context", "b")]))

    def test_proxies_alone_never_cover_a_chapter(self):
        import scrape_v3
        self.assertFalse(scrape_v3.chapter_search_satisfied(
            self._chapter(),
            [self._window("editorial_proxy", "a"), self._window("editorial_proxy", "b")]))

    def test_the_new_rejections_are_named_in_the_grading_prompt(self):
        """The live path matches the model's reject STRING against V3_REJECTIONS, so a category
        the prompt never mentions can never be returned."""
        from pathlib import Path
        import scrape_v3
        source = Path(scrape_v3.__file__).read_text(encoding="utf-8")
        for name in ("screen_recording", "text_dominates", "talking_head", "duet_or_stitch"):
            self.assertIn(name, scrape_v3.V3_REJECTIONS)
            self.assertIn(name + " means", source,
                          f"{name} is a category the grader is never told about")


class BeatCoverageTests(unittest.TestCase):
    """The coverage check counted SECONDS while the assignment hands out one window per BEAT.
    A 9.2s chapter with four beats was therefore declared covered by three windows, the search
    stopped, and the run died at the final audit with an uncovered beat - after every chance to
    look for more footage had already passed."""

    def _chapter(self, beats, span):
        import scrape_v3
        return scrape_v3.Chapter(chapter_id=0, title="t",
                                 scene_ids=[str(i) for i in range(beats)],
                                 start=0.0, end=span, subject="s", action="a", evidence="e",
                                 proxies_ok=True, forbidden=[], queries=[],
                                 allow_multi_window=True, is_hook=False)

    def _windows(self, count, seconds=4.0):
        import scrape_v3
        return [scrape_v3.ShotWindow(chapter_id=0, source_id=f"s{i}", platform="tiktok",
                                     path="p.mp4", start=0.0, end=seconds, match_class="exact")
                for i in range(count)]

    def test_a_chapter_is_not_covered_with_fewer_windows_than_beats(self):
        import scrape_v3
        self.assertFalse(scrape_v3.chapter_search_satisfied(
            self._chapter(4, 9.2), self._windows(3)))

    def test_one_window_per_beat_is_enough(self):
        import scrape_v3
        self.assertTrue(scrape_v3.chapter_search_satisfied(
            self._chapter(4, 9.2), self._windows(4)))

    def test_plenty_of_windows_still_pass(self):
        import scrape_v3
        self.assertTrue(scrape_v3.chapter_search_satisfied(
            self._chapter(3, 5.8), self._windows(16)))


class RepeatedShotTests(unittest.TestCase):
    """A delivered Short opened on three near-identical departure boards. The repeat guard was
    scoped per SOURCE, so three different uploads of the same picture were three sources and
    passed - but a viewer sees one shot three times."""

    def _chapter(self, beats):
        import scrape_v3
        return scrape_v3.Chapter(chapter_id=0, title="t",
                                 scene_ids=[str(i) for i in range(beats)],
                                 start=0.0, end=12.0, subject="s", action="a", evidence="e",
                                 proxies_ok=True, forbidden=[], queries=[],
                                 allow_multi_window=True, is_hook=False)

    def _graded(self, rows):
        return [{"source_id": source, "platform": "tiktok", "path": "p.mp4", "query": "q",
                 "source_has_captions": False,
                 "windows": [{"start": 0.0, "end": 4.0, "match_class": "exact",
                              "reason": "r", "visual_signature": signature}]}
                for source, signature in rows]

    BOARD = "board | shows times | station"
    POOL = [("a", BOARD), ("b", BOARD), ("c", BOARD),
            ("d", "cleaner | wipes seats | carriage")]

    def test_the_same_picture_from_different_posts_is_used_once(self):
        import scrape_v3
        chosen = scrape_v3.select_chapter_windows(self._chapter(2), self._graded(self.POOL))
        signatures = [w.visual_signature for w in chosen]
        self.assertEqual(len(signatures), len(set(signatures)), signatures)

    def test_a_repeat_still_beats_an_uncovered_beat(self):
        """The audit refuses a beat with no clip, so variety gives way when the chapter cannot
        otherwise be filled."""
        import scrape_v3
        chosen = scrape_v3.select_chapter_windows(self._chapter(4), self._graded(self.POOL))
        self.assertEqual(len(chosen), 4)


class CaptionPreferenceTests(unittest.TestCase):
    """Refusing every clip the remover cannot fully clean emptied whole chapters: Japanese TikTok
    is saturated with burned-in text, and two runs failed with watchable footage sitting unused.
    Clean footage is preferred; captioned footage fills a beat only when nothing cleaner exists."""

    def test_the_relaxation_order_gives_ground_one_step_at_a_time(self):
        from pathlib import Path
        import scrape_v3
        source = Path(scrape_v3.__file__).read_text(encoding="utf-8")
        block = source[source.index("chosen, skipped = pick(across_sources=True)"):]
        block = block[:block.index("captioned = sum(")]
        # clean-and-varied, then repeats, then text, and only last the whole-Short source
        # budget - never text before variety, never the budget before either.
        self.assertIn("(False, False, False), (True, True, False)", block)
        self.assertIn("(False, True, False), (False, True, True)", block)

    def test_the_hopeless_cases_are_still_refused_outright(self):
        """Past twice the ceiling the picture IS the text; no amount of need makes it usable."""
        from pathlib import Path
        import scrape_v3
        source = Path(scrape_v3.__file__).read_text(encoding="utf-8")
        self.assertIn("if share > 2 * CAPTION_CEILING:", source)

    def test_a_chapter_that_settles_for_text_says_so(self):
        from pathlib import Path
        import scrape_v3
        source = Path(scrape_v3.__file__).read_text(encoding="utf-8")
        self.assertIn("still carry creator text the ", source,
                      "settling for captioned footage has to be reported, not silent")

    def test_the_caption_compromise_is_not_limited_to_exact_matches(self):
        """Restricting it to exact matches was measured and reverted: all three topics attempted
        under that rule failed, against two deliveries in the two runs before it. Relevance is
        defended by the chapter grounding rule, not by the caption ceiling."""
        from pathlib import Path
        import scrape_v3
        source = Path(scrape_v3.__file__).read_text(encoding="utf-8")
        self.assertNotIn('allow_captioned and window.match_class == "exact"', source)
