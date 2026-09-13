"""TikTok media can only be fetched by the browser that asked for it.

Measured on 2026-08-27 against real records from the paid API:

    yt-dlp over the page                        0/3  ("Unexpected response from webpage request")
    signed CDN link, 4 header combinations      0/3  (403 each: plain, browser UA, +Referer, +Range)
    logged-in context, bytes from ITS response  3/3  (720x1280 and 576x1024, 4-8s each)

The "link read then re-GET" row of that table was measured with an OUTSIDE request, and it became
a rule that the media must never be requested again. Re-measured 2026-09-03 on the pufferfish run:
harvesting the browser's own response is unreliable by design, because Chrome evicts large media
bodies and `Response.body()` then answers "No data found" - 49 of 116 sources were lost that way.
A request through the browser CONTEXT, which carries its cookies, served all 14 re-checked posts
(4.9 MB to 70 MB). So the harvest is now the fast path and the re-fetch is the answer when Chrome
no longer has the bytes; behaviour is covered in test_tiktok_media_refetch.py.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SessionDownloadTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "tiktok_login.py").read_text(encoding="utf-8")

    def test_the_session_can_download(self):
        import tiktok_login
        self.assertTrue(hasattr(tiktok_login, "download_sync"))
        self.assertTrue(hasattr(tiktok_login.Session, "download_video"))

    def test_the_page_s_own_response_is_still_tried_first(self):
        """When Chrome does still hold the bytes, taking them saves a whole second download."""
        block = self.source[self.source.index("def download_video("):]
        block = block[:block.index("def _hydration_items")]
        self.assertIn("body = resp.body()", block)

    def test_an_evicted_body_is_re_fetched_through_the_context(self):
        """Not through a bare HTTP client: only the context carries the session's cookies."""
        block = self.source[self.source.index("def download_video("):]
        block = block[:block.index("def _hydration_items")]
        self.assertIn("self._ctx.request.get(", block)

    def test_the_player_is_nudged_into_asking_for_the_media(self):
        """Without a play intent the page never requests the file at all."""
        block = self.source[self.source.index("def download_video("):]
        self.assertIn("v.play()", block[:3000])
        self.assertIn("v.muted = true", block[:3000])

    def test_a_block_page_is_not_written_as_a_video(self):
        block = self.source[self.source.index("def download_video("):]
        block = block[:block.index("def _hydration_items")]
        self.assertIn("len(body) > 40000", block)
        self.assertIn('b"ftyp" in body[:4096]', block)

    def test_the_page_is_always_closed(self):
        block = self.source[self.source.index("def download_video("):]
        block = block[:block.index("def _hydration_items")]
        self.assertIn("finally:", block)
        self.assertIn("page.close()", block)

    def test_all_playwright_work_stays_on_the_one_worker_thread(self):
        """A session built on one thread and used from another is what poisoned job threads."""
        self.assertIn("def _download_on_worker(", self.source)
        self.assertIn("_executor().submit(_download_on_worker", self.source)


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "scrape_v2.py").read_text(encoding="utf-8")

    def test_the_downloader_uses_the_session_for_tiktok(self):
        self.assertIn("tiktok_login.download_sync(url, dest", self.source)

    def test_instagram_keeps_the_faster_direct_link(self):
        block = self.source[self.source.index("def download_proxy_v2("):]
        direct_at = block.index("_download_direct_media(direct, dest")
        session_at = block.index("tiktok_login.download_sync")
        self.assertLess(direct_at, session_at,
                        "the direct CDN link works for Instagram and is faster")

    def test_a_dead_session_falls_through_instead_of_losing_the_clip(self):
        block = self.source[self.source.index("def download_proxy_v2("):]
        block = block[:block.index("h = int(SCRAPE_V2_CONFIG")]
        self.assertIn("except Exception:", block)
        self.assertIn("if tiktok_login.is_ready():", block)


if __name__ == "__main__":
    unittest.main()
