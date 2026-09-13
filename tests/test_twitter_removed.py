"""X/Twitter is gone, and nothing may quietly bring it back.

Removed 2026-09-03 on request: "wirf btw twitter komplett raus, funktioniert ja eh nie". It was a
second logged-in backend searched in parallel with TikTok, with its own query validator, a
zero-streak health probe that disabled it mid-run, a quarter-scale like floor and its own Connect
control. Every query spent against it was a query not spent on TikTok.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core
import app
import clip_scraper
import scrape_v2
import scrape_v3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TheModuleIsGone(unittest.TestCase):

    def test_the_login_module_no_longer_exists(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "twitter_login.py")))

    def test_nothing_imports_it(self):
        for name in ("clip_scraper.py", "app.py", "agent_core.py", "run_native.py",
                     "scrape_v2.py", "scrape_v3.py", "chat_ui.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("import twitter_login", source, name)
            self.assertNotIn("twitter_login.", source, name)

    def test_the_scraper_has_no_x_backend_left(self):
        self.assertFalse(hasattr(clip_scraper, "twitter_backend_ready"))
        self.assertFalse(hasattr(clip_scraper, "twitter_login"))
        self.assertFalse(hasattr(clip_scraper, "is_valid_x_query"))
        self.assertFalse(hasattr(clip_scraper, "x_queries_for_search"))


class NoPlatformResolvesToX(unittest.TestCase):

    def test_an_old_project_asking_for_x_gets_tiktok(self):
        """Saved projects and presets still carry platform lists naming twitter or x."""
        for value in (["twitter"], ["x"], ["x.com"], "twitter", ["tiktok", "twitter"]):
            with self.subTest(value=value):
                self.assertEqual(clip_scraper.normalize_platforms(value), {"tiktok"})

    def test_the_default_is_tiktok_alone(self):
        self.assertEqual(clip_scraper.normalize_platforms(None), {"tiktok"})

    def test_instagram_still_resolves(self):
        self.assertEqual(clip_scraper.normalize_platforms(["instagram"]), {"instagram"})
        self.assertEqual(clip_scraper.normalize_platforms(["tiktok", "ig"]),
                         {"tiktok", "instagram"})

    def test_the_like_floor_has_no_x_scale(self):
        self.assertEqual(clip_scraper.platform_like_floor(20_000, "twitter"), 20_000)
        self.assertEqual(clip_scraper.platform_like_floor(20_000, "instagram"), 10_000)


class TheUiOffersNoConnectX(unittest.TestCase):

    def test_no_connect_control_or_route(self):
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for marker in ("Connect X", "/twitter-status", "/twitter-login",
                       "connectTwitter", "twitter_status_payload"):
            self.assertNotIn(marker, source, marker)

    def test_the_chat_shell_lists_three_connections(self):
        with open(os.path.join(ROOT, "static", "chat-shell.js"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("/twitter-status", source)


if __name__ == "__main__":
    unittest.main()
