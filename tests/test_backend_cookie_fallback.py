"""yt-dlp must get the login cookies that are already on disk.

Measured 2026-09-02: `generated_assets/tiktok/tiktok_cookies.txt` held all five TikTok session
cookies, `tiktok_backend_ready()` was True, and `_merge_backend_cookies(["tiktok"])` still
returned None - because `export_cookies_txt()` returns None whenever the live session object
cannot hand its cookies over in this process. So `_COOKIES_SPEC` stayed None and yt-dlp ran
ANONYMOUSLY through every scrape, meeting TikTok's bot-check page instead of the video.

Note what this did NOT fix: on a sample of eight real post URLs the download success rate was
4/8 both before and after, so the anonymous yt-dlp was not the cause of those failures.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clip_scraper


NETSCAPE = ("# Netscape HTTP Cookie File\n"
            ".tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc123\n"
            ".tiktok.com\tTRUE\t/\tTRUE\t0\tsid_tt\tdef456\n")


def fake_login(tmp_path, text, exported=None):
    module = types.SimpleNamespace()
    module.COOKIES_TXT = str(tmp_path)
    module.SESSION_COOKIE_NAMES = ("sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt")
    module.export_cookies_txt = lambda: exported
    if text is not None:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
    return module


class CookieFallback(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="cookiefb_")
        self.path = os.path.join(self.dir, "tiktok_cookies.txt")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_saved_file_is_used_when_the_live_session_gives_nothing(self):
        module = fake_login(self.path, NETSCAPE, exported=None)
        self.assertIn("sessionid", clip_scraper._session_cookie_text(module))

    def test_a_file_without_a_session_cookie_is_not_treated_as_a_login(self):
        module = fake_login(self.path, ".tiktok.com\tTRUE\t/\tTRUE\t0\ttheme\tdark\n",
                            exported=None)
        self.assertEqual(clip_scraper._session_cookie_text(module), "")

    def test_a_missing_file_is_not_an_error(self):
        module = fake_login(self.path, None, exported=None)
        self.assertEqual(clip_scraper._session_cookie_text(module), "")

    def test_the_live_session_still_wins_when_it_has_something(self):
        live = os.path.join(self.dir, "live.txt")
        with open(live, "w", encoding="utf-8") as handle:
            handle.write("# Netscape HTTP Cookie File\n"
                         ".tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tLIVE\n")
        module = fake_login(self.path, NETSCAPE, exported=live)
        self.assertIn("LIVE", clip_scraper._session_cookie_text(module))


if __name__ == "__main__":
    unittest.main()
