"""Offline unit tests for the TikTok Search Lab pure logic (no live session needed).
Run: python test_tiktok_benchmark.py
"""
import json
import tempfile
from pathlib import Path

import scrape_v2
from tools import tiktok_search_benchmark as B

_fail = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fail.append(name)


def test_dataset():
    check("at least 30 visual intents", len(B.INTENTS) >= 30)
    cats = {i.category for i in B.INTENTS}
    for c in ("school", "convenience_store", "vending_machine", "train", "daily_life", "work",
              "unusual_japan", "hook_presenter"):
        check("category present: " + c, c in cats)
    check("every intent has must_show + avoid", all(i.must_show and i.avoid for i in B.INTENTS))
    check("intent ids unique", len({i.intent_id for i in B.INTENTS}) == len(B.INTENTS))


def test_query_taxonomy():
    it = next(i for i in B.INTENTS if i.intent_id == "school_cleaning_01")
    q = B.build_queries(it)
    produced = [t for t in B.QUERY_TYPES if q.get(t)]
    check("at least 10 query types produced", len(produced) >= 10)
    check("subject_action_location present", bool(q["subject_action_location"]))
    check("english control present", bool(q["english"]))
    check("hashtag present", bool(q["hashtag"]))
    check("presenter_style empty for non-hook", not q["presenter_style"])
    hk = B.build_queries(next(i for i in B.INTENTS if i.category == "hook_presenter"))
    check("presenter_style present for hook", bool(hk["presenter_style"]))
    # diversity: queries within a type are not near-identical
    for t, qs in q.items():
        for a in range(len(qs)):
            for b in range(a + 1, len(qs)):
                check("distinct queries in %s" % t, B.normalize_query(qs[a]) != B.normalize_query(qs[b]))


def _seg(cap_prob, reasons=None):
    return scrape_v2.SegmentCandidate(segment_id="s", source_id="x", platform="tiktok", source_path="",
                                      start_time=0, end_time=2.3, duration=2.3,
                                      caption_probability=cap_prob, rejection_reasons=reasons or [])


def test_metrics():
    # 10 raw results; 5 downloaded+analysed. 3 relevant (A/B, overall>=6.5), 4 usable segments.
    raw = []
    for i in range(10):
        raw.append({"result_position": i + 1, "platform_id": "id%d" % i, "creator_id": "c%d" % (i % 4),
                    "caption": "", "hashtags": [], "likes": 1000 * (i + 1), "duration": 12.0,
                    "width": 720, "height": 1280, "is_slideshow": False})
    per_source = []
    plan = [
        (0, 8.5, "A_MATCH", True, 8, 7, 7),   # relevant + usable + exact
        (1, 7.2, "B_MATCH", True, 6, 6, 6),   # relevant + usable
        (2, 6.8, "A_MATCH", True, 8, 6, 6),   # relevant + usable + exact
        (3, 4.0, "D_REJECTED", True, 2, 2, 2),# usable segment but not relevant
        (4, 3.0, "D_REJECTED", False, 1, 1, 1),
    ]
    for (idx, overall, mc, usable, am, sm, lm) in plan:
        r = raw[idx]
        r.update({"overall_match": overall, "match_class": mc, "has_usable_segment": usable,
                  "action_match": am, "subject_match": sm, "location_match": lm, "best_quality": 7.0,
                  "_best_seg": _seg(0.1)})
        per_source.append(r)
    m = B._compute_query_metrics(raw, per_source, elapsed=5.0, cfg=B.BenchConfig())
    check("relevant_at_10 = 0.3 (3 of top10 relevant)", m["relevant_at_10"] == 0.3)
    check("usable_at_10 = 0.4 (4 of top10 usable)", m["usable_at_10"] == 0.4)
    check("exact_match_rate = 0.2 (2 exact in top10)", m["exact_match_rate"] == 0.2)
    # final_yield = relevant AND usable segments / raw_results(10) = 3/10
    check("final_yield = 0.3", m["final_yield"] == 0.3)
    check("creator_diversity computed (<=1)", 0 < m["creator_diversity"] <= 1)
    check("vertical_rate = 1.0 (all portrait)", m["vertical_rate"] == 1.0)


def test_aggregate_and_cross_dup():
    rows = [
        {"category": "school", "query_type": "native_vlog", "query": "a",
         "raw": [{"platform_id": "V1"}, {"platform_id": "V2"}],
         "metrics": {"relevant_at_10": 0.6, "usable_at_10": 0.7, "final_yield": 0.3, "exact_match_rate": 0.2,
                     "search_efficiency": 0.1, "caption_free_rate": 0.8, "vertical_rate": 0.9,
                     "usable_segment_rate": 0.6, "creator_diversity": 0.7, "download_success_rate": 0.9,
                     "duplicate_rate": 0.1, "average_search_time": 3.0}},
        {"category": "school", "query_type": "hashtag", "query": "b",
         "raw": [{"platform_id": "V1"}, {"platform_id": "V9"}],   # V1 shared -> cross-query dup
         "metrics": {"relevant_at_10": 0.3, "usable_at_10": 0.3, "final_yield": 0.1, "exact_match_rate": 0.1,
                     "search_efficiency": 0.05, "caption_free_rate": 0.4, "vertical_rate": 0.8,
                     "usable_segment_rate": 0.3, "creator_diversity": 0.5, "download_success_rate": 0.8,
                     "duplicate_rate": 0.2, "average_search_time": 4.0}},
    ]
    ts, cts, cross = B._aggregate(rows)
    check("per-type summary built", "native_vlog" in ts and "hashtag" in ts)
    check("native_vlog yield > hashtag yield", ts["native_vlog"]["final_yield"] > ts["hashtag"]["final_yield"])
    check("cross-query duplicate detected (V1 in 2 queries)", cross > 0)
    check("per category|type summary", any(k.startswith("school|") for k in cts))


def test_reports_from_cache():
    with tempfile.TemporaryDirectory() as td:
        run_id = "bench_test"
        cache = Path(td) / run_id / "cache"
        cache.mkdir(parents=True)
        # two cached query results
        for i, qt in enumerate(("native_vlog", "hashtag")):
            (cache / f"q{i}.json").write_text(json.dumps({
                "category": "school", "query_type": qt, "query": "q%d" % i,
                "raw": [{"platform_id": "P%d" % i}],
                "metrics": {"relevant_at_10": 0.6 - i * 0.3, "usable_at_10": 0.5, "final_yield": 0.3 - i * 0.2,
                            "exact_match_rate": 0.1, "search_efficiency": 0.1, "caption_free_rate": 0.5,
                            "vertical_rate": 0.9, "usable_segment_rate": 0.5, "creator_diversity": 0.6,
                            "download_success_rate": 0.9, "duplicate_rate": 0.1, "average_search_time": 3.0}}),
                encoding="utf-8")
        old = B.BENCH_DIR
        B.BENCH_DIR = Path(td)
        try:
            rep = B.build_reports(run_id)
        finally:
            B.BENCH_DIR = old
        rd = Path(td) / run_id
        check("report.json written", (rd / "report.json").exists())
        check("report.csv written", (rd / "report.csv").exists())
        check("report.html written", (rd / "report.html").exists())
        check("ranking sorted by final_yield (native_vlog first)",
              rep["query_type_ranking"][0]["query_type"] == "native_vlog")
        check("recommendations derived (best overall present)",
              bool(rep["recommendations"]["best_overall_query_types"]))


def test_v1_v2_untouched():
    import agent_core, clip_scraper
    check("V1 scrape_social_plan intact", hasattr(agent_core, "scrape_social_plan"))
    check("V2 scrape_social_plan_v2 intact", hasattr(scrape_v2, "scrape_social_plan_v2"))
    check("clip_scraper.backend_search intact", hasattr(clip_scraper, "backend_search"))


if __name__ == "__main__":
    for t in (test_dataset, test_query_taxonomy, test_metrics, test_aggregate_and_cross_dup,
              test_reports_from_cache, test_v1_v2_untouched):
        print("\n== %s ==" % t.__name__)
        try:
            t()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            _fail.append(t.__name__ + " raised " + repr(exc))
    print("\n" + ("ALL PASSED" if not _fail else "FAILURES: %d -> %s" % (len(_fail), _fail)))
    raise SystemExit(1 if _fail else 0)
