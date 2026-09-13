"""Scrape.do rendered TikTok discovery: URLs only, attributed, priced, and never leaking the key.

The module deliberately stops at canonical post URLs. V4 then opens only those posts through the
user's own TikTok session - it must never issue a local keyword search of its own.
"""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

import scrapedo_tiktok as sd


SEARCH_HTML = """
<div><a href="https://www.tiktok.com/@tokyo.walks/video/7412345678901234567">a</a>
<a href="/@tokyo.walks/video/7412345678901234567">same post again</a>
<a href="https://www.tiktok.com/@Tokyo.Walks/video/7412345678901234567">same, other case</a>
<script>{"url":"https:\\u002F\\u002Fwww.tiktok.com\\u002F@ramen_daily\\u002Fvideo\\u002F7498765432109876543"}</script>
<script>{"u":"https:\\/\\/www.tiktok.com\\/@station_pov\\/video\\/7455555555555555555"}</script>
<a href="https://www.tiktok.com/@brand/video/12345">too short an id</a>
<a href="https://www.tiktok.com/@someone/live">not a post</a>
&lt;a href=&quot;https://www.tiktok.com/@escaped.user/video/7466666666666666666&quot;&gt;
"""


class ExtractionTests(unittest.TestCase):
    def urls(self):
        return sd.extract_post_urls(SEARCH_HTML)

    def test_it_returns_canonical_post_urls(self):
        for url in self.urls():
            self.assertRegex(url, r"^https://www\.tiktok\.com/@[A-Za-z0-9._-]+/video/\d{6,}$")

    def test_the_same_post_is_returned_once(self):
        """The rendered page repeats a post as an absolute link, a relative one and a
        differently-cased handle. Three hits, one post."""
        ids = [u.rsplit("/", 1)[1] for u in self.urls()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sum(i == "7412345678901234567" for i in ids), 1)

    def test_it_reads_json_escaped_links(self):
        """Most of a rendered TikTok page carries its links inside JSON blobs."""
        ids = [u.rsplit("/", 1)[1] for u in self.urls()]
        self.assertIn("7498765432109876543", ids)     # \\u002F escaped
        self.assertIn("7455555555555555555", ids)     # \\/ escaped

    def test_it_reads_html_escaped_links(self):
        self.assertIn("7466666666666666666", [u.rsplit("/", 1)[1] for u in self.urls()])

    def test_it_ignores_non_posts_and_short_ids(self):
        joined = " ".join(self.urls())
        self.assertNotIn("/live", joined)
        self.assertNotIn("/video/12345", joined)

    def test_empty_markup_is_not_a_crash(self):
        self.assertEqual(sd.extract_post_urls(""), [])
        self.assertEqual(sd.extract_post_urls(None), [])


class RequestShapeTests(unittest.TestCase):
    def test_the_target_url_is_a_real_tiktok_search(self):
        target = sd.target_search_url("日本 自販機")
        self.assertTrue(target.startswith("https://www.tiktok.com/search?q="))
        self.assertIn("%E6%97%A5%E6%9C%AC", target)

    def test_the_browser_scrolls_a_bounded_number_of_times(self):
        """Three scrolls, and no setting may ask for more.

        Scrape.do measured this on their side 2026-09-02: 3 scrolls returned ~14 videos in ~28s,
        6 scrolls returned the same ~14 in ~38s, and 10 timed out and returned nothing. Coverage
        comes from more search TERMS, not from scrolling one term deeper - which is what this
        pipeline had independently measured as a hard ~12 URL ceiling per query.
        """
        # 0 falls back to the default (the module reads `scroll_steps or 3`); anything higher
        # clamps down to 3 rather than spending the extra ten seconds for nothing.
        for steps, expected in ((1, 1), (3, 3), (0, 3), (6, 3), (99, 3)):
            actions = sd.browser_actions(steps)
            self.assertEqual(sum(a.get("Action") == "Execute" for a in actions), expected)

    def test_it_waits_before_the_first_scroll(self):
        self.assertEqual(sd.browser_actions(2)[0]["Action"], "Wait")


class CostTests(unittest.TestCase):
    """The Scrape.do-Request-Cost header must survive into the report.

    Reading it only for a status line meant a finished run could not be priced from its own
    artefacts - and a status line scrolls away.
    """

    def test_a_record_carries_the_cost_and_the_geo(self):
        records = _fake_search("ramen", cost="5", geo="jp")
        self.assertTrue(records)
        self.assertEqual(records[0]["_scrapedo_cost"], "5")
        self.assertEqual(records[0]["_scrapedo_geo"], "jp")

    def test_cost_is_counted_once_per_request_not_per_url(self):
        """Two queries, several URLs each: the charge is per rendered request."""
        grouped = {"a": _fake_search("a", cost="5"), "b": _fake_search("b", cost="5")}
        self.assertGreater(len(grouped["a"]), 1)
        self.assertEqual(sd.total_cost(grouped), 10.0)

    def test_a_missing_header_costs_nothing_rather_than_crashing(self):
        self.assertEqual(sd.total_cost({"a": _fake_search("a", cost="")}), 0.0)
        self.assertEqual(sd.total_cost({}), 0.0)
        self.assertEqual(sd.total_cost([]), 0.0)

    def test_it_also_accepts_a_flat_record_list(self):
        flat = _fake_search("a", cost="5") + _fake_search("b", cost="5")
        self.assertEqual(sd.total_cost(flat), 10.0)


class AttributionTests(unittest.TestCase):
    def test_every_record_names_the_query_that_found_it(self):
        """V4 assigns a candidate to a beat only if its query belongs to that beat."""
        for record in _fake_search("japanese vending machine", cost="5"):
            self.assertEqual(record["_search_query"], "japanese vending machine")
            self.assertEqual(record["_platform"], "tiktok")
            self.assertEqual(record["_source"], "scrapedo_tiktok")

    def test_search_many_dedupes_queries_before_paying_for_them(self):
        seen = []

        def fake(query, **kwargs):
            seen.append(query)
            return _records(query, cost="5")

        with mock.patch.object(sd, "search", side_effect=fake):
            out = sd.search_many(["ramen", "Ramen", " ramen ", "station"])
        self.assertEqual(len(seen), 2, seen)
        self.assertEqual(set(out), {"ramen", "station"})

    def test_one_failed_query_does_not_lose_the_others(self):
        def fake(query, **kwargs):
            if query == "boom":
                raise RuntimeError("Scrape.do TikTok search failed (HTTPError)")
            return _records(query, cost="5")

        with mock.patch.object(sd, "search", side_effect=fake):
            out = sd.search_many(["boom", "ok"])
        self.assertEqual(out["boom"], [])
        self.assertTrue(out["ok"])


class SecretTests(unittest.TestCase):
    def test_the_key_is_never_hardcoded(self):
        source = Path(sd.__file__).read_text(encoding="utf-8")
        self.assertIn('os.environ.get("SCRAPEDO_API_KEY"', source)
        # the only occurrences may be the env lookups, never a literal
        self.assertNotIn("token=", source.replace('"token": key', ""))

    def test_a_missing_key_refuses_instead_of_calling_out(self):
        with mock.patch.object(sd, "_api_key", return_value=""):
            self.assertFalse(sd.available())
            with self.assertRaises(RuntimeError):
                sd.search("anything")

    def test_the_key_is_not_echoed_in_an_error(self):
        """A traceback or status line must never carry the token."""
        with mock.patch.object(sd, "_api_key", return_value="SECRET-TOKEN-VALUE"), \
                mock.patch.object(sd.urllib.request, "urlopen", side_effect=OSError("boom")):
            with self.assertRaises(RuntimeError) as caught:
                sd.search("ramen")
        self.assertNotIn("SECRET-TOKEN-VALUE", str(caught.exception))

    def test_no_project_or_repo_file_contains_the_live_key(self):
        # Read it the way the module does: on Windows the key lives in the User registry, not in
        # the process environment, so an os.environ-only check silently skipped this test.
        key = sd._api_key()
        if not key or len(key) < 12:
            self.skipTest("no live key in this environment")
        root = Path(__file__).resolve().parent.parent
        for name in ("scrapedo_tiktok.py", "scrape_v4.py", "agent_core.py", "app.py"):
            path = root / name
            if path.is_file():
                self.assertNotIn(key, path.read_text(encoding="utf-8", errors="replace"), name)


def _records(query, cost="5", geo="jp"):
    """Records in the exact shape `search()` returns - built WITHOUT calling it.

    The first version reused _fake_search here; in the tests that patch `sd.search`, that
    recursed into the patch (330 calls before the pool gave up).
    """
    return [{"id": url.rsplit("/", 1)[1], "webVideoUrl": url, "url": url, "desc": "",
             "_platform": "tiktok", "_source": "scrapedo_tiktok", "_search_query": str(query),
             "_scrapedo_cost": cost, "_scrapedo_geo": geo}
            for url in sd.extract_post_urls(SEARCH_HTML)]


def _fake_search(query, cost="5", geo="jp"):
    """Run the real `search()` against a canned response - no network, no key."""
    class Response:
        headers = {"Scrape.do-Request-Cost": cost}

        def read(self):
            return SEARCH_HTML.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with mock.patch.object(sd, "_api_key", return_value="test-key"), \
            mock.patch.object(sd.urllib.request, "urlopen", return_value=Response()):
        return sd.search(query, geo_code=geo)


if __name__ == "__main__":
    unittest.main()
