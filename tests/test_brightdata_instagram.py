"""Instagram discovery through Bright Data is ACCOUNT-based, and that is not a shortcut.

Asked for its discovery types, the Reels dataset answers `url, url_all_reels`; Posts answers
`url`; and the two datasets that would search - "Instagram posts search by keyword" and
"Instagram hashtag information" - both answer "This dataset does not support collection". A
hashtag page URL and an /explore/search/?q= URL were each tried against the reels collector and
both returned the same single unrelated reel, attributed to "explore". So a topic reaches
Instagram as a list of creators who post that topic, and an explore-page row is refused rather
than passed off as a hit.
"""

import json
import unittest

import brightdata_instagram as ig


def reel(**over):
    base = {"url": "https://www.instagram.com/reel/DbV6f3lh-hO/",
            "post_id": "3951321524774365262", "shortcode": "DbV6f3lh-hO",
            "user_posted": "visitjapanjp", "user_profile_url": "https://www.instagram.com/visitjapanjp",
            "description": "Ready for a coastal adventure", "hashtags": ["#japan"],
            "likes": 1022, "video_play_count": 19223, "views": 19223, "length": 35.6,
            "followers": 124171049, "date_posted": "2026-07-28T16:00:25.000Z"}
    base.update(over)
    return base


class ProfileUrlTests(unittest.TestCase):
    def test_every_way_of_naming_an_account_reaches_the_same_url(self):
        for value in ("visitjapanjp", "@visitjapanjp", "https://www.instagram.com/visitjapanjp",
                      "https://www.instagram.com/visitjapanjp/"):
            self.assertEqual(ig.profile_url(value), "https://www.instagram.com/visitjapanjp")

    def test_a_query_string_is_dropped(self):
        self.assertEqual(ig.profile_url("https://www.instagram.com/x/?hl=en"),
                         "https://www.instagram.com/x")

    def test_nothing_in_nothing_out(self):
        for value in ("", None, "   "):
            self.assertEqual(ig.profile_url(value), "")


class ResponseShapeTests(unittest.TestCase):
    """The same endpoint answers as NDJSON, as a JSON array, or as a snapshot id to poll."""

    def test_ndjson_is_parsed(self):
        raw = "\n".join(json.dumps(reel(post_id=str(i))) for i in range(3))
        self.assertEqual(len(ig._rows(raw)), 3)

    def test_a_plain_json_array_is_parsed(self):
        self.assertEqual(len(ig._rows(json.dumps([reel(), reel()]))), 2)

    def test_a_snapshot_envelope_survives(self):
        rows = ig._rows(json.dumps({"snapshot_id": "sd_x", "message": "still in progress"}))
        self.assertEqual(rows[0]["snapshot_id"], "sd_x")

    def test_an_empty_body_is_not_an_error(self):
        """A non-existent account answers HTTP 200 with nothing in it."""
        for raw in ("", "   ", None):
            self.assertEqual(ig._rows(raw), [])

    def test_one_broken_line_does_not_lose_the_rest(self):
        raw = json.dumps(reel()) + "\n{ not json\n" + json.dumps(reel(post_id="2"))
        self.assertEqual(len(ig._rows(raw)), 2)


class NormaliseTests(unittest.TestCase):
    def test_a_real_reel_becomes_a_pipeline_item(self):
        item = ig._normalise(reel())
        self.assertEqual(item["_platform"], "instagram")
        self.assertEqual(item["id"], "3951321524774365262")
        self.assertEqual(item["stats"]["diggCount"], 1022)
        self.assertEqual(item["stats"]["playCount"], 19223)
        self.assertEqual(item["video"]["duration"], 35600)
        self.assertGreater(item["createTime"], 1_700_000_000)

    def test_an_explore_page_row_is_refused(self):
        """This is what a hashtag or search URL returns: unrelated footage that looks like a hit."""
        self.assertIsNone(ig._normalise(reel(user_posted="explore")))

    def test_a_row_with_no_author_is_refused(self):
        self.assertIsNone(ig._normalise(reel(user_posted="", user_profile_url="")))

    def test_the_url_is_rebuilt_from_the_shortcode_when_missing(self):
        item = ig._normalise(reel(url=""))
        self.assertEqual(item["webVideoUrl"], "https://www.instagram.com/reel/DbV6f3lh-hO/")

    def test_a_row_with_no_identity_at_all_is_refused(self):
        self.assertIsNone(ig._normalise({"description": "x"}))

    def test_the_shape_is_left_unknown_rather_than_guessed(self):
        item = ig._normalise(reel())
        self.assertEqual((item["video"]["width"], item["video"]["height"]), (0, 0))

    def test_junk_in_a_numeric_field_costs_that_field_only(self):
        item = ig._normalise(reel(likes="lots", length="a while"))
        self.assertEqual(item["likes"] if "likes" in item else item["stats"]["diggCount"], 0)
        self.assertEqual(item["video"]["duration"], 0)
        self.assertEqual(item["id"], "3951321524774365262")

    def test_hashtags_survive_only_when_they_are_a_list(self):
        self.assertEqual(ig._normalise(reel(hashtags=None))["hashtags"], [])
        self.assertEqual(ig._normalise(reel(hashtags=["#a", "#b"]))["hashtags"], ["#a", "#b"])


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(ig.reset_budget)
        ig.reset_budget()

    def test_the_budget_starts_empty_and_is_bounded(self):
        self.assertEqual(ig.records_used(), 0)
        self.assertGreater(ig.RECORD_BUDGET, 0)

    def test_no_accounts_means_no_request(self):
        self.assertEqual(ig.reels_for_accounts([], status_cb=None), [])
        self.assertEqual(ig.records_used(), 0)


if __name__ == "__main__":
    unittest.main()


class AccountPickerTests(unittest.TestCase):
    """A topic reaches Instagram as a list of creators, because that is the only door open."""

    def setUp(self):
        import scrape_v3
        self.v3 = scrape_v3
        self.source = open(scrape_v3.__file__, encoding="utf-8").read()

    def _pick(self, answer):
        real = self.v3.scrape_v2._llm_json
        try:
            self.v3.scrape_v2._llm_json = lambda *a, **k: answer
            return self.v3.instagram_accounts_for("japan trains", [], reasoning_model=None)
        finally:
            self.v3.scrape_v2._llm_json = real

    def test_plain_handles_come_through(self):
        self.assertEqual(self._pick({"accounts": ["visitjapanjp", "tokyofashion"]}),
                         ["visitjapanjp", "tokyofashion"])

    def test_an_at_sign_or_a_url_is_reduced_to_the_handle(self):
        """A model asked for handles will sometimes answer with URLs anyway."""
        self.assertEqual(self._pick({"accounts": ["@visitjapanjp",
                                                  "https://www.instagram.com/tokyofashion/?hl=en"]}),
                         ["visitjapanjp", "tokyofashion"])

    def test_duplicates_are_not_paid_for_twice(self):
        self.assertEqual(self._pick({"accounts": ["visitjapanjp", "VisitJapanJP"]}),
                         ["visitjapanjp"])

    def test_anything_that_is_not_a_handle_is_dropped(self):
        """A wrong handle costs a paid call and returns an empty body."""
        self.assertEqual(self._pick({"accounts": ["a b c", "", None, "way-too-illegal!",
                                                  "x" * 40, "ok.handle_1"]}),
                         ["ok.handle_1"])

    def test_a_dead_model_call_leaves_the_run_on_tiktok_only(self):
        real = self.v3.scrape_v2._llm_json
        try:
            def boom(*a, **k):
                raise RuntimeError("no balance")
            self.v3.scrape_v2._llm_json = boom
            self.assertEqual(self.v3.instagram_accounts_for("x", []), [])
        finally:
            self.v3.scrape_v2._llm_json = real

    def test_a_nonsense_answer_shape_is_survivable(self):
        for answer in (None, {}, {"accounts": None}, {"accounts": "visitjapanjp"}, []):
            self.assertIsInstance(self._pick(answer), list)

    def test_the_picker_runs_once_per_project_not_per_query(self):
        self.assertIn("clip_scraper.set_instagram_accounts(", self.source)
        self.assertEqual(self.source.count("instagram_accounts_for("), 2,
                         "defined once, called once")

    def test_it_is_skipped_when_instagram_was_not_asked_for(self):
        self.assertIn('if "instagram" in {str(p).strip().lower() for p in (platforms or ())}:',
                      self.source)


class AccountPickerQualityTests(unittest.TestCase):
    """The picker must not name the official operator's account.

    On a Tokyo train-pushers short it chose jreast_official, tokyometro and odakyu - the railway
    companies themselves. Their reels are adverts and announcements: a sampled nine came back as
    a sakura picnic, a tea commercial, boots, a beach and two talking heads, and the vision pass
    rejected all of them. The footage this pipeline needs is filmed by passengers, not operators.
    """

    def setUp(self):
        import scrape_v3
        self.prompt = scrape_v3.INSTAGRAM_ACCOUNTS_PROMPT

    def test_official_brand_accounts_are_ruled_out(self):
        self.assertIn("RAW FOOTAGE, NOT PUBLICITY", self.prompt)
        self.assertIn("Never name the official account", self.prompt)

    def test_it_says_who_to_name_instead(self):
        for who in ("POV vloggers", "commuters", "tourists"):
            self.assertIn(who, self.prompt)

    def test_the_measurement_that_motivated_it_is_recorded(self):
        self.assertIn("train-pushers short", self.prompt)

    def test_the_handle_only_rule_survived(self):
        self.assertIn("Handles only, no URLs", self.prompt)
