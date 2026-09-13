"""An Instagram retry must actually be a different search.

`backend_search` falls back to a native hashtag when a keyword search returns nothing, because
Instagram's keyword search often answers with the Home feed. The fallback built that tag from
the query's strongest CJK token - and when the query WAS already that tag, it rebuilt the same
string and ran the identical search a second time.

Every empty Japanese hashtag query therefore cost two searches instead of one, on the backend
whose empty results the health check is counting. A suite test had been failing on exactly this
for the whole session (3 expected, 6 observed) and was being read as test pollution.
"""

import unittest

import clip_scraper


class FallbackTests(unittest.TestCase):
    def test_a_query_that_is_already_the_tag_has_no_fallback(self):
        for query in ("#名頃かかしの里", "#お見舞いマナー", "#すっぴんメイク", "#カプセルホテル"):
            self.assertEqual(clip_scraper._instagram_hashtag_fallback(query), "", query)

    def test_a_phrase_still_falls_back_to_its_strongest_tag(self):
        self.assertEqual(clip_scraper._instagram_hashtag_fallback("カプセルホテル 大浴場"),
                         "#カプセルホテル")
        self.assertEqual(clip_scraper._instagram_hashtag_fallback("ゴミ収集車 走行"),
                         "#ゴミ収集車")

    def test_generic_scaffolding_is_still_skipped(self):
        """Prefer the concrete concept over geography."""
        self.assertEqual(clip_scraper._instagram_hashtag_fallback("日本 給食 当番"), "#給食")

    def test_a_latin_query_has_no_native_tag(self):
        self.assertEqual(clip_scraper._instagram_hashtag_fallback("#japan"), "")
        self.assertEqual(clip_scraper._instagram_hashtag_fallback("capsule hotel tour"), "")

    def test_case_and_whitespace_do_not_defeat_the_check(self):
        self.assertEqual(clip_scraper._instagram_hashtag_fallback("  #カプセルホテル  "), "")


class SearchCountTests(unittest.TestCase):
    def test_an_empty_hashtag_query_costs_exactly_one_search(self):
        import concurrent.futures
        from unittest import mock
        clip_scraper.reset_backend_search_health()
        self.addCleanup(clip_scraper.reset_backend_search_health)
        seen = []

        def empty(query, *_args, **_kwargs):
            seen.append(query)
            future = concurrent.futures.Future()
            future.set_result([])
            return future

        with mock.patch.object(clip_scraper, "instagram_backend_ready", return_value=True), \
             mock.patch.object(clip_scraper, "tiktok_backend_ready", return_value=False), \
             mock.patch.object(clip_scraper.instagram_login, "search_async", side_effect=empty), \
             mock.patch.object(clip_scraper.instagram_login, "health_check",
                               return_value={"ok": False}):
            clip_scraper.backend_search("#名頃かかしの里", 5, platforms=["instagram"])
        self.assertEqual(seen, ["#名頃かかしの里"])


if __name__ == "__main__":
    unittest.main()
