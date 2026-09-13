"""The paid fallback must not be ranked out of the chapters it exists to rescue.

Once TikTok reports its daily search limit, Bright Data is the only source left. Its payload
carries `width` and a `ratio` class but NO height field, so every record reached the ranker with
height 0 - and the resolution term is guarded by `if c.width and c.height`, which meant a flat
0 of 10 for the fallback while a login result scored up to 10. The clips were not worse; their
metadata was thinner.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import brightdata_tiktok as bd                                          # noqa: E402
import scrape_v2 as v2                                                  # noqa: E402


def record(**over):
    base = {"post_id": "7452495577994071327", "account_id": "someone",
            "url": "https://www.tiktok.com/@someone/video/7452495577994071327",
            "description": "train pushers at rush hour", "width": 720, "ratio": "720p",
            "video_duration": 14, "digg_count": 236, "play_count": 871,
            "create_time": "2026-06-06T09:05:47.000Z"}
    base.update(over)
    return base


class NormaliseTests(unittest.TestCase):
    def test_a_real_payload_normalises(self):
        item = bd._normalise(record())
        self.assertEqual(item["id"], "7452495577994071327")
        self.assertEqual(item["stats"]["diggCount"], 236)

    def test_a_missing_height_is_left_unknown_rather_than_invented(self):
        """Claiming 9:16 for every record would silence the landscape penalty for good."""
        item = bd._normalise(record())
        self.assertEqual(item["video"]["height"], 0)
        self.assertEqual(item["video"]["width"], 720)

    def test_the_quality_class_is_carried_through(self):
        self.assertEqual(bd._normalise(record())["video"]["ratio"], "720p")

    def test_a_real_height_is_still_used_when_present(self):
        item = bd._normalise(record(height=1280))
        self.assertEqual(item["video"]["height"], 1280)

    def test_an_iso_timestamp_becomes_epoch_seconds(self):
        self.assertGreater(bd._normalise(record())["createTime"], 1_700_000_000)


class ShapeScoreTests(unittest.TestCase):
    def test_an_unknown_shape_scores_neutrally(self):
        self.assertGreater(v2.UNKNOWN_SHAPE_SCORE, 0.0,
                           "absent evidence was being scored as bad evidence")
        self.assertLess(v2.UNKNOWN_SHAPE_SCORE, 10.0)

    def test_it_sits_among_the_ordinary_not_above_the_best(self):
        """A 1080x1920 upload scores ~9.5 and a 720-wide one ~5.5 on the same scale."""
        full_hd = max(0.0, min(10.0, (1920 - 400) / 160.0))
        modest = max(0.0, min(10.0, (1280 - 400) / 160.0))
        self.assertLess(v2.UNKNOWN_SHAPE_SCORE, full_hd)
        self.assertLessEqual(v2.UNKNOWN_SHAPE_SCORE, modest)

    def test_a_measured_landscape_clip_is_still_penalised(self):
        source = (ROOT / "scrape_v2.py").read_text(encoding="utf-8")
        block = source[source.index("res = UNKNOWN_SHAPE_SCORE"):]
        self.assertIn("if c.height < c.width:", block[:400])
        self.assertIn("res *= 0.4", block[:400])


class CallTimingTests(unittest.TestCase):
    """Measured live: submitted at t+0, `running` until t+103, `ready` at t+111, 10 records."""

    def setUp(self):
        self.source = (ROOT / "clip_scraper.py").read_text(encoding="utf-8")

    def test_the_call_is_given_longer_than_a_job_actually_takes(self):
        import re
        cap = float(re.search(r"^BRIGHTDATA_CALL_TIMEOUT_S = ([\d.]+)",
                              self.source, re.M).group(1))
        self.assertGreaterEqual(cap, 200.0, "this cap kills the call just before it pays out")

    def test_a_call_is_not_started_without_time_to_finish(self):
        import re
        floor = float(re.search(r"^BRIGHTDATA_MIN_REMAINING_S = ([\d.]+)",
                                self.source, re.M).group(1))
        self.assertGreaterEqual(floor, 120.0)

    def test_a_bright_data_bound_run_gets_a_longer_chapter(self):
        v3 = (ROOT / "scrape_v3.py").read_text(encoding="utf-8")
        self.assertIn("chapter_cap = 420.0 if tiktok_daily_limited() else 150.0", v3)
        self.assertIn("def tiktok_daily_limited():", v3)


if __name__ == "__main__":
    unittest.main()


class BatchedKeywordTests(unittest.TestCase):
    """Eight serial jobs spend the chapter clock on two queries; one batched job spends one wait.

    A discover job is 60-110s of queueing whatever the keyword count, and the endpoint accepts an
    input ARRAY - so the whole chapter is submitted at once and rows come back attributed via the
    `discovery_input` stamp.
    """

    def setUp(self):
        self.source = (ROOT / "brightdata_tiktok.py").read_text(encoding="utf-8")
        self.v3 = (ROOT / "scrape_v3.py").read_text(encoding="utf-8")

    def test_the_whole_chapter_is_one_job(self):
        self.assertIn("def search_many(", self.source)
        self.assertIn("clip_scraper.brightdata_tiktok.search_many(", self.v3)

    def test_rows_are_attributed_by_discovery_input(self):
        self.assertIn('source.get("search_keyword")', self.source)

    def test_an_unattributed_row_is_kept_not_dropped(self):
        """It was paid for; a missing provenance stamp must not cost the record."""
        self.assertIn("keyword if keyword in out else wanted[0]", self.source)

    def test_a_batched_keyword_is_never_billed_twice(self):
        block = self.v3[self.v3.index("if api_results is not None:"):]
        self.assertIn('if sort_mode != "RELEVANCE":\n                    return', block[:700])
        self.assertIn("items = list(api_results.get(query) or [])", block[:900])

    def test_the_response_may_be_ndjson(self):
        """A cached result returns finished records as NDJSON where a fresh submit returns a
        snapshot envelope - json.loads alone made the cache look like an outage."""
        self.assertIn("def _parse_body(", self.source)
        import brightdata_tiktok as bd
        ndjson = '{"a": 1}\n{"b": 2}\n'
        self.assertEqual(bd._parse_body(ndjson), [{"a": 1}, {"b": 2}])
        self.assertEqual(bd._parse_body('{"snapshot_id": "x"}'), {"snapshot_id": "x"})
        self.assertIsNone(bd._parse_body(""))

    def test_the_batch_respects_the_record_budget(self):
        self.assertIn("wanted = wanted[:max(1, room // per_query)]", self.source)

    def test_search_many_survives_no_queries(self):
        import brightdata_tiktok as bd
        self.assertEqual(bd.search_many([]), {})
        self.assertEqual(bd.search_many(["", None]), {})


class DeadInputTests(unittest.TestCase):
    """An input that dies on the provider side is not an input that found nothing.

    Measured live: a four-keyword job came back `records: 12, errors: 2,
    error_codes: {"dead_page": 2}`. The two dead inputs arrive as ROWS carrying an `error` or
    `warning` field, so parsing them yielded nothing and the keyword was reported as empty -
    which is why the same query returned 0 one minute and 10 the next.
    """

    def setUp(self):
        self.source = (ROOT / "brightdata_tiktok.py").read_text(encoding="utf-8")

    def test_an_error_row_is_never_parsed_as_a_clip(self):
        self.assertEqual(self.source.count('if row.get("error") or row.get("warning"):'), 2,
                         "both the single and the batched path must skip provider errors")

    def test_a_dead_input_is_named_rather_than_counted_as_empty(self):
        self.assertIn("were NOT searched, which is", self.source)

    def test_a_dead_input_is_retried_once(self):
        self.assertIn("retry=False", self.source)
        self.assertIn("if empty and retry and room_left >= per_query:", self.source)

    def test_the_retry_cannot_recurse(self):
        import brightdata_tiktok as bd
        import inspect
        self.assertIn("retry", inspect.signature(bd.search_many).parameters)
        block = self.source[self.source.index("if empty and retry and room_left >= per_query:"):]
        self.assertIn("retry=False", block[:500])

    def test_the_retry_respects_the_remaining_budget(self):
        self.assertIn("room = BRIGHTDATA_RECORD_BUDGET - _RECORDS_USED", self.source)


class RecordFairShareTests(unittest.TestCase):
    """One chapter may not eat the whole record budget.

    Measured on the sushi run: chapter 0's jobs took 70 of the 60-record budget - charging
    happened only when records ARRIVED, so a second job started while the first was counting and
    the ceiling was overshot by a whole job. Chapters 1 and 2 then searched with nothing left:
    2 and 1 raw results against chapter 0's 60, and six of ten beats shipped as cards.
    """

    def setUp(self):
        self.bd = (ROOT / "brightdata_tiktok.py").read_text(encoding="utf-8")
        self.v3 = (ROOT / "scrape_v3.py").read_text(encoding="utf-8")

    def test_the_budget_is_reserved_at_submit(self):
        self.assertIn("reserved = len(wanted) * per_query", self.bd)
        self.assertIn("_RECORDS_USED += reserved", self.bd)

    def test_dead_inputs_are_not_billed(self):
        self.assertIn("_RECORDS_USED += found - reserved", self.bd)

    def test_a_caller_can_cap_one_call(self):
        import brightdata_tiktok as bd
        import inspect
        self.assertIn("max_records", inspect.signature(bd.search_many).parameters)

    def test_each_chapter_gets_its_share(self):
        self.assertIn("chapters_left = max(1, int(getattr(chapter, \"chapters_remaining\"", self.v3)
        self.assertIn("max_records=share", self.v3)

    def test_the_share_follows_the_same_rule_as_the_time_budget(self):
        """Reserve for the untouched chapters, exactly like fair_seconds does."""
        self.assertIn("chapter.chapters_remaining = untouched", self.v3)

    def test_reservation_accounting_nets_to_reality(self):
        import brightdata_tiktok as bd
        bd.reset_budget()
        # simulate: reserve 20, get 7 back -> used must be 7, not 20 and not 27
        bd._RECORDS_USED += 20          # the reserve step
        bd._RECORDS_USED += 7 - 20      # the reconcile step
        self.assertEqual(bd.records_used(), 7)
        bd.reset_budget()
