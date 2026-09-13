"""An API record already contains a direct link to the file; use it.

The first API-only run discovered 104 results and downloaded 2. Everything else went through
yt-dlp against the reel PAGE - which means negotiating a logged-out Instagram, the exact thing
this mode exists to skip. The payload carries `video_url` (Instagram) and `cdn_url`/`video_url`
(TikTok), and fetching that returned playable 720x1280 files on the first try.
"""

import tempfile
import unittest
from pathlib import Path

import brightdata_instagram as ig
import brightdata_tiktok as bd
import scrape_v2 as v2


class CarriedLinkTests(unittest.TestCase):
    def test_instagram_carries_its_cdn_link(self):
        item = ig._normalise({"url": "https://www.instagram.com/reel/X/", "post_id": "1",
                              "user_posted": "someone", "length": 10,
                              "video_url": "https://scontent.cdninstagram.com/v/a.mp4"})
        self.assertEqual(item["_media_url"], "https://scontent.cdninstagram.com/v/a.mp4")

    def test_tiktok_carries_its_cdn_link(self):
        item = bd._normalise({"post_id": "1", "account_id": "someone",
                              "url": "https://www.tiktok.com/@someone/video/1",
                              "video_url": "https://v16-webapp-prime.tiktok.com/video/x.mp4"})
        self.assertEqual(item["_media_url"], "https://v16-webapp-prime.tiktok.com/video/x.mp4")

    def test_a_record_without_one_is_still_usable(self):
        item = ig._normalise({"url": "https://www.instagram.com/reel/X/", "post_id": "1",
                              "user_posted": "someone"})
        self.assertEqual(item["_media_url"], "")


class DirectDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="direct_"))

    def test_a_dead_link_falls_back_instead_of_failing(self):
        """Signed CDN links expire; an expired one is a reason to try the normal path."""
        got = v2._download_direct_media("https://example.invalid/nope.mp4", self.tmp / "a.mp4")
        self.assertIsNone(got)

    def test_a_link_that_is_not_a_link_is_ignored(self):
        for url in ("", "not a url", "ftp://x/y.mp4"):
            self.assertIsNone(v2._download_direct_media(url, self.tmp / "b.mp4"))

    def test_an_error_page_is_not_mistaken_for_a_video(self):
        """A CDN answering an expired link with HTML still writes a file."""
        source = open(v2.__file__, encoding="utf-8").read()
        block = source[source.index("def _download_direct_media"):]
        block = block[:block.index("def download_proxy_v2")]
        self.assertIn("st_size < 40000", block)
        self.assertIn("dest.unlink(missing_ok=True)", block)

    def test_the_proxy_size_cap_still_applies(self):
        source = open(v2.__file__, encoding="utf-8").read()
        block = source[source.index("def _download_direct_media"):]
        self.assertIn('SCRAPE_V2_CONFIG["proxy_max_filesize_mb"]', block[:1600])

    def test_the_downloader_tries_the_direct_link_first(self):
        source = open(v2.__file__, encoding="utf-8").read()
        block = source[source.index("def download_proxy_v2"):]
        head = block[:block.index("h = int(SCRAPE_V2_CONFIG")]
        self.assertIn('direct = str(item.get("_media_url") or "")', head)
        self.assertIn("if got:\n            return got", head)


class ApiBoundBudgetTests(unittest.TestCase):
    """With no browser session there is nothing to report a daily limit - and the chapter clock
    was reading exactly that flag to decide whether it had time for an async API call."""

    def test_an_api_only_run_counts_as_api_bound(self):
        import clip_scraper
        import scrape_v3
        was = clip_scraper.BRIGHTDATA_ONLY
        try:
            clip_scraper.set_brightdata_only(True)
            self.assertTrue(scrape_v3.tiktok_daily_limited())
        finally:
            clip_scraper.set_brightdata_only(was)

    def test_a_normal_run_still_asks_the_browser(self):
        import clip_scraper
        import scrape_v3
        was = clip_scraper.BRIGHTDATA_ONLY
        try:
            clip_scraper.set_brightdata_only(False)
            self.assertIsInstance(scrape_v3.tiktok_daily_limited(), bool)
        finally:
            clip_scraper.set_brightdata_only(was)


if __name__ == "__main__":
    unittest.main()
