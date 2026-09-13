"""Contract checks for the isolated Bright-only V4 engine."""
from pathlib import Path
import base64
import unittest

import scrape_v4 as v4


class V4ContractTests(unittest.TestCase):
    def test_queries_are_action_led_and_never_name_a_platform(self):
        scene = {"exact_voice_text": "Japanese students wipe their classroom desks.",
                 "visual_subject": "Japanese classroom", "visual_action": "students wipe desks"}
        queries = v4._queries(scene, "School cleaning")
        self.assertTrue(queries)
        self.assertFalse(any("tiktok" in query.lower() or "instagram" in query.lower()
                             for query in queries))

    def test_first_bright_round_reserves_alternatives_for_uncovered_beats(self):
        tasks = [
            (0, {}, ["first subject", "first action", "first native", "first recovery"]),
            (1, {}, ["second subject", "second action", "second native", "second recovery"]),
        ]
        first, deferred = v4._initial_query_round(tasks, per_beat=3)
        self.assertEqual(first, ["first subject", "first action", "first native",
                                 "second subject", "second action", "second native"])
        self.assertEqual(deferred, ["first recovery", "second recovery"])

    def test_bright_record_merge_deduplicates_post_ids_across_query_rounds(self):
        records, seen = [], set()
        first = {"a": [{"id": "11", "_platform": "tiktok"}]}
        second = {"b": [{"id": "11", "_platform": "tiktok"},
                        {"id": "22", "_platform": "tiktok"}]}
        self.assertEqual(v4._merge_bright_grouped(records, first, seen), 1)
        self.assertEqual(v4._merge_bright_grouped(records, second, seen), 1)
        self.assertEqual([(q, row["id"]) for q, row in records], [("a", "11"), ("b", "22")])

    def test_v4_reserves_and_uses_a_second_bright_pass_only_for_uncovered_beats(self):
        source = Path(v4.__file__).read_text(encoding="utf-8")
        self.assertIn("_initial_query_round(tasks, per_beat=3)", source)
        # The recovery round is provider-neutral now (Scrape.do is the default discovery and
        # Bright the fallback), so it logs "V4 discovery recovery" rather than naming Bright.
        # What the test is really about is that a SECOND pass exists, is reserved for uncovered
        # beats, and reaches both providers.
        self.assertIn("V4 discovery recovery", source)
        self.assertIn("recovery_wanted", source)
        recovery = source[source.index("V4 discovery recovery"):]
        recovery = recovery[:recovery.index("second_inspected")]
        self.assertIn("scrapedo_tiktok.search_many", recovery)
        self.assertIn("brightdata_tiktok.search_many", recovery)

    def test_v4_has_no_v2_v3_or_direct_browser_or_resolver_dependency(self):
        source = Path(v4.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import scrape_v3", source)
        self.assertNotIn("import scrape_v2", source)
        self.assertNotIn("playwright", source)
        self.assertNotIn("download_proxy", source)
        self.assertIn("_media_url", source)
        self.assertIn("BRIGHTDATA_UNLOCKER_ZONE", source)
        self.assertIn("https://api.brightdata.com/request", source)
        self.assertIn("ThreadPoolExecutor", source)
        self.assertIn("v4_download_workers", source)
        self.assertIn("authenticated_fallback", source)

    def test_short_or_overlapping_windows_cannot_be_reused(self):
        # Short clean shots can serve shorter beats; assignment must reject a
        # three-second beat that this source cannot cover at the speed floor.
        short = v4._windows(1.9, 3.0)
        self.assertTrue(short)
        self.assertTrue(all(end - start < 3.0 * v4.V4_STRETCH_FLOOR for start, end in short))
        windows = v4._windows(12.0, 7.0)
        self.assertGreaterEqual(len(windows), 2)
        self.assertGreater(windows[1][0] - windows[0][0], 2.75)
        self.assertLessEqual(max(end - start for start, end in windows), 2.751)

    def test_v4_uses_the_requested_compact_multimodal_reviewer(self):
        self.assertEqual(v4.DEFAULT_V4_VISION_MODEL, "google/gemini-3.7-flash")
        self.assertEqual(v4.DEFAULT_V4_TTS_MODEL, "pro")
        self.assertEqual(v4.DEFAULT_V4_TTS_VOICE, "Laomedeia")
        self.assertEqual(v4.DEFAULT_V4_TTS_SPEAKER, "Narrator")

    def test_instagram_profiles_are_a_separate_bright_collector_input(self):
        self.assertEqual(v4._clean_instagram_accounts("@one, https://www.instagram.com/two/"),
                         ["one", "two"])
        source = Path(v4.__file__).read_text(encoding="utf-8")
        self.assertIn("url_all_reels", source)
        self.assertIn("brightdata_instagram", source)
        self.assertIn("v4_instagram_enabled", source)


class UnlockerEnvelopeTests(unittest.TestCase):
    class Reply:
        def __init__(self, content, envelope=None):
            self.content, self.envelope = content, envelope
        def json(self):
            if self.envelope is None:
                raise ValueError("not json")
            return self.envelope

    def test_raw_mp4_is_not_reencoded(self):
        raw = b"\x00\x00\x00\x18ftypisom" + b"x" * 100
        self.assertEqual(v4._unlocker_video_bytes(self.Reply(raw), {}), raw)

    def test_base64_video_envelope_becomes_mp4_bytes(self):
        raw = b"\x00\x00\x00\x18ftypisom" + b"x" * 100
        response = self.Reply(b"{}", {"status_code": 200,
            "headers": {"content-type": "video/mp4"}, "body": base64.b64encode(raw).decode()})
        self.assertEqual(v4._unlocker_video_bytes(response, {}), raw)

    def test_non_video_envelope_is_rejected_before_timeline(self):
        item = {}
        response = self.Reply(b"{}", {"status_code": 200,
            "headers": {"content-type": "text/html"}, "body": "<html>blocked</html>"})
        self.assertEqual(v4._unlocker_video_bytes(response, item), b"")
        self.assertIn("not video", item["_bright_download_error"])


if __name__ == "__main__":
    unittest.main()
