"""Running entirely on the paid API, with no logged-in browser sessions at all.

This is an operating mode, not a fallback. TikTok's own search reached its daily ceiling on the
FIRST query of a batch and every later browser search was dead weight; in this mode no session is
refreshed, no worker thread is started, and nothing waits on a login wall.

Instagram has no keyword search on this account, so it is served from a pool of reels pulled once
from the creators the run names, and each query takes only the reels that actually answer it.
"""

import unittest

import clip_scraper as cs


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(cs.set_brightdata_only, False)
        self.addCleanup(cs.set_instagram_accounts, [])

    def test_it_is_off_by_default(self):
        cs.set_brightdata_only(False)
        self.assertFalse(cs.BRIGHTDATA_ONLY)

    def test_it_can_be_switched_at_runtime_and_by_environment(self):
        self.assertTrue(cs.set_brightdata_only(True))
        self.assertFalse(cs.set_brightdata_only(False))
        source = open(cs.__file__, encoding="utf-8").read()
        self.assertIn('os.environ.get("SCRAPE_BACKEND", "")', source)

    def test_the_api_key_is_the_backend_in_this_mode(self):
        """backend_active gates the whole run; checking for a login here would refuse to start."""
        cs.set_brightdata_only(True)
        source = open(cs.__file__, encoding="utf-8").read()
        block = source[source.index("def backend_active("):]
        block = block[:block.index("def backend_name(")]
        self.assertIn("if BRIGHTDATA_ONLY:", block)
        self.assertIn("brightdata_tiktok.available()", block)

    def test_no_browser_backend_is_touched(self):
        source = open(cs.__file__, encoding="utf-8").read()
        block = source[source.index("    tt_ready = ("):]
        block = block[:block.index("ig_future = None") + 400]
        self.assertIn("if not BRIGHTDATA_ONLY and \"tiktok\" in selected", block)
        self.assertIn("if not BRIGHTDATA_ONLY and \"instagram\" in selected", block)

    def test_the_api_runs_as_primary_not_only_after_a_limit(self):
        source = open(cs.__file__, encoding="utf-8").read()
        self.assertIn("if ((BRIGHTDATA_ONLY or (not items and tt_daily_limited))", source)

    def test_the_record_budget_still_caps_the_bill(self):
        source = open(cs.__file__, encoding="utf-8").read()
        self.assertIn("_BRIGHTDATA_RECORDS_USED < BRIGHTDATA_RECORD_BUDGET", source)


class InstagramPoolTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(cs.set_instagram_accounts, [])
        self.addCleanup(cs.set_brightdata_only, False)

    def test_accounts_are_normalised_and_kept(self):
        self.assertEqual(cs.set_instagram_accounts(["@a", " b ", "", None, "c"]),
                         ["@a", "b", "c"])

    def test_no_accounts_means_no_pool_and_no_spend(self):
        cs.set_instagram_accounts([])
        self.assertEqual(cs.instagram_pool(), [])

    def test_setting_accounts_clears_a_stale_pool(self):
        cs.set_instagram_accounts(["@a"])
        cs._IG_POOL = [{"id": "cached"}]
        cs.set_instagram_accounts(["@b"])
        self.assertIsNone(cs._IG_POOL)

    def test_the_pool_is_paid_for_once_and_reused(self):
        calls = []
        real = cs.brightdata_instagram
        try:
            class Fake:
                @staticmethod
                def reels_for_accounts(accounts, per_account=6, status_cb=None, timeout_s=300):
                    calls.append(list(accounts))
                    return [{"id": "1"}, {"id": "2"}]
            cs.brightdata_instagram = Fake
            cs.set_instagram_accounts(["@a", "@b"])
            self.assertEqual(len(cs.instagram_pool()), 2)
            self.assertEqual(len(cs.instagram_pool()), 2)
            self.assertEqual(len(calls), 1, "the pool was fetched - and billed - twice")
        finally:
            cs.brightdata_instagram = real

    def test_only_reels_that_answer_the_query_are_served(self):
        """An account pool is topical, not per-beat: handing every beat the same top reel would
        fill a Short with one clip."""
        source = open(cs.__file__, encoding="utf-8").read()
        block = source[source.index('if BRIGHTDATA_ONLY and "instagram" in selected:'):]
        self.assertIn("if score > 0.0", block[:2400])


if __name__ == "__main__":
    unittest.main()


class PoolCrowdingTests(unittest.TestCase):
    """The pool must not eat the download slots the keyword search paid for.

    Measured on a real run: the same account pool was offered 135 times - once per query - so the
    round-robin that hands out download slots saw those reels in every bucket. Result: 30 of 30
    downloaded proxies were pooled Instagram reels and not one TikTok keyword hit was ever
    fetched. Of nine sampled pooled reels, exactly one showed the subject; the rest were a sakura
    picnic, a tea advert, boots, a beach and two talking heads.
    """

    def setUp(self):
        self.source = open(cs.__file__, encoding="utf-8").read()
        self.addCleanup(cs.set_instagram_accounts, [])

    def test_a_pooled_reel_is_offered_once_per_run(self):
        self.assertIn("_IG_SERVED", self.source)
        self.assertIn('str(entry.get("id") or "") not in _IG_SERVED', self.source)

    def test_the_pool_takes_a_minority_of_any_query_s_slots(self):
        self.assertIn("keep[:max(1, int(want) // 4)]", self.source)

    def test_new_accounts_reset_what_has_been_served(self):
        cs.set_instagram_accounts(["@a"])
        cs._IG_SERVED.add("x")
        cs.set_instagram_accounts(["@b"])
        self.assertEqual(cs._IG_SERVED, set())

    def test_the_log_says_how_much_of_the_pool_is_spent(self):
        self.assertIn("of the pool used", self.source)
