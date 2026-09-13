"""Full post-processing analysis of a completed (or partial) TikTok Search Lab run.

Pure post-processing of the run's per-query cache - it NEVER re-runs a query, so it is safe to run
on a still-in-progress run (partial) or a finished one. Produces analysis.json + analysis.html with:
failure taxonomy, per-category rankings, download/vision counts + a cost estimate, and a per-query
top-10 review table (to spot-check vision judgments).

    python -m tools.bench_report <run_id>
"""
import json
import sys

from tools.tiktok_search_benchmark import (BENCH_DIR, _load_cache, _aggregate, _derive_recommendations,
                                           INTENTS)

_INTENT_BY_ID = {i.intent_id: i for i in INTENTS}

# Cost model (ESTIMATE - WaveSpeed exposes no per-call price API here). Vision = GPT-5.5 w/ 1 image.
VISION_COST_PER_CALL_USD = 0.02          # midpoint estimate; range shown as [0.5x, 2x]
RELEVANT_FLOOR = 6.5


def _downloaded(raw):
    return [r for r in raw if "download_success" in r]


def failure_taxonomy(rows):
    """Separate: dead query / results-but-download-failed / usable-segment-missing /
    usable-but-vision-rejected / accepted. Tallied globally + per query type."""
    glob = {"dead_query": 0, "zero_yield_query": 0, "download_failed": 0, "no_usable_segment": 0,
            "usable_but_vision_rejected": 0, "accepted": 0, "not_evaluated_budget": 0}
    by_type = {}
    for r in rows:
        m = r.get("metrics") or {}
        raw = r.get("raw") or []
        qt = r.get("query_type", "?")
        t = by_type.setdefault(qt, {k: 0 for k in glob})
        if not raw or m.get("raw_results", 0) == 0:
            glob["dead_query"] += 1; t["dead_query"] += 1
            continue
        accepted_here = 0
        dled = _downloaded(raw)
        for x in dled:
            if not x.get("download_success"):
                glob["download_failed"] += 1; t["download_failed"] += 1
            elif not x.get("has_usable_segment"):
                glob["no_usable_segment"] += 1; t["no_usable_segment"] += 1
            elif str(x.get("match_class")) == "D_REJECTED" or float(x.get("overall_match", 0)) < RELEVANT_FLOOR:
                glob["usable_but_vision_rejected"] += 1; t["usable_but_vision_rejected"] += 1
            else:
                glob["accepted"] += 1; t["accepted"] += 1; accepted_here += 1
        not_eval = len(raw) - len(dled)
        glob["not_evaluated_budget"] += not_eval; t["not_evaluated_budget"] += not_eval
        if accepted_here == 0:
            glob["zero_yield_query"] += 1; t["zero_yield_query"] += 1
    return glob, by_type


def cost_and_counts(rows):
    downloads_attempted = downloads_ok = segments = vision_calls = 0
    for r in rows:
        raw = r.get("raw") or []
        dled = _downloaded(raw)
        downloads_attempted += len(dled)
        downloads_ok += sum(1 for x in dled if x.get("download_success"))
        seg = sum(int(x.get("segments", 0)) for x in dled)
        segments += seg
        vision_calls += (-(-seg // 6)) + (1 if seg > 0 else 0)   # ceil(seg/6) describe batches + 1 match
    est = vision_calls * VISION_COST_PER_CALL_USD
    return {"downloads_attempted": downloads_attempted, "downloads_ok": downloads_ok,
            "segments_discovered": segments, "vision_calls_est": vision_calls,
            "vision_cost_usd_est": round(est, 2),
            "vision_cost_usd_range": [round(est * 0.5, 2), round(est * 2.0, 2)],
            "cost_note": "Estimate: WaveSpeed has no per-call price API here; assumes "
                         f"~${VISION_COST_PER_CALL_USD}/vision call (GPT-5.5 + 1 image). Search + "
                         "yt-dlp downloads are free."}


def relevance_recalibrated(rows, thresholds=(4.5, 6.0), k=10):
    """Re-derive relevance PER QUERY TYPE from the cached per-result overall_match scores at several
    thresholds (the fixed graded scorer runs on a compressed 0-5ish scale, so the 6.0 floor is too
    strict - 4.5 = topically relevant). Also reports avg_overall_match + unscored-segment count (a
    reliability caveat: segments the describe step failed to score, which unfairly land at 0)."""
    by_type = {}
    for r in rows:
        raw = [x for x in (r.get("raw") or []) if "download_success" in x]
        if not raw:
            continue
        top = raw[:k]
        scored = [x for x in raw if x.get("overall_match") is not None]
        rec = by_type.setdefault(r.get("query_type", "?"),
                                 {"n": 0, "avg_overall_match": [], "best_overall": [],
                                  "unscored_downloaded": 0, "downloaded": 0,
                                  **{f"rel@{k}_ge{t}": [] for t in thresholds}})
        rec["n"] += 1
        rec["downloaded"] += len(raw)
        # a downloaded result that got overall==0 AND action None = describe failed to score it
        rec["unscored_downloaded"] += sum(1 for x in raw if x.get("action_match") is None)
        overs = [float(x.get("overall_match") or 0) for x in raw]
        rec["avg_overall_match"].append(round(sum(overs) / max(1, len(overs)), 2))
        rec["best_overall"].append(max(overs) if overs else 0.0)
        for t in thresholds:
            rec[f"rel@{k}_ge{t}"].append(round(sum(1 for x in top if float(x.get("overall_match") or 0) >= t) / max(1, len(top)), 3))
    out = {}
    for qt, rec in by_type.items():
        o = {"n": rec["n"], "downloaded": rec["downloaded"], "unscored_downloaded": rec["unscored_downloaded"]}
        for key, vals in rec.items():
            if isinstance(vals, list) and vals:
                o["avg_" + key if key in ("avg_overall_match", "best_overall") else key] = round(sum(vals) / len(vals), 3)
        out[qt] = o
    return out


def category_rankings(rows):
    cats = {}
    for r in rows:
        m = r.get("metrics") or {}
        if not m:
            continue
        cats.setdefault(r.get("category", "?"), {}).setdefault(r.get("query_type", "?"), []).append(m)
    out = {}
    for c, tmap in cats.items():
        ranked = []
        for qt, ms in tmap.items():
            def avg(k):
                vals = [float(x.get(k, 0)) for x in ms if k in x]
                return round(sum(vals) / len(vals), 3) if vals else 0.0
            ranked.append({"query_type": qt, "n": len(ms), "relevant_at_10": avg("relevant_at_10"),
                           "usable_at_10": avg("usable_at_10"), "final_yield": avg("final_yield"),
                           "caption_free_rate": avg("caption_free_rate"),
                           "exact_match_rate": avg("exact_match_rate")})
        ranked.sort(key=lambda x: (x["final_yield"], x["usable_at_10"]), reverse=True)
        out[c] = ranked
    return out


def top10_review_html(rows, max_queries=None):
    """Full review: EVERY raw result per query (in TikTok's original order), with the visual intent,
    the script/scene text, the vision score + technical verdict. Auto-rejected clips are shown too."""
    blocks = []
    for r in (rows[:max_queries] if max_queries else rows):
        m = r.get("metrics") or {}
        raw = (r.get("raw") or [])            # ALL results, not just top-10
        it = _INTENT_BY_ID.get(r.get("intent_id"))
        intent_line = (f'<div style="color:#555;font-size:12px">visual intent: <b>{it.subject}</b> / '
                       f'<b>{it.action}</b> / <b>{it.location}</b> &nbsp;&middot;&nbsp; scene: '
                       f'&laquo;{it.scene_text}&raquo; &nbsp;&middot;&nbsp; avoid: {", ".join(it.avoid)}</div>'
                       if it else "")
        head = (f"<h3>{r.get('category')} &middot; <code>{r.get('query_type')}</code> &middot; "
                f"&laquo;{r.get('query')}&raquo; <small>(raw {m.get('raw_results',0)}, "
                f"rel@10 {m.get('relevant_at_10',0)}, usable@10 {m.get('usable_at_10',0)}, "
                f"avg-match {m.get('avg_overall_match',0)}, yield {m.get('final_yield',0)})</small></h3>"
                + intent_line)
        trs = []
        for x in raw:
            dl = x.get("download_success")
            mc = x.get("match_class", "-") if dl else "-"
            om = x.get("overall_match", "-") if dl else "-"
            us = ("yes" if x.get("has_usable_segment") else "no") if dl else "-"
            reason = ("no download" if dl is False else
                      ("no usable segment" if dl and not x.get("has_usable_segment") else
                       ("vision rejected" if dl and (str(mc) == "D_REJECTED" or (isinstance(om, (int, float)) and om < RELEVANT_FLOOR)) else
                        ("ACCEPTED" if dl else "not evaluated"))))
            cap = (x.get("caption") or "")[:60].replace("<", "&lt;")
            trs.append(f"<tr><td>{x.get('result_position')}</td><td>{str(x.get('creator_id',''))[:16]}</td>"
                       f"<td>{cap}</td><td>{x.get('likes',0):,}</td>"
                       f"<td>{x.get('width',0)}x{x.get('height',0)}</td><td>{x.get('duration',0):.0f}s</td>"
                       f"<td>{mc}</td><td>{om}</td><td>{us}</td><td>{reason}</td></tr>")
        blocks.append(head + "<table><tr><th>#</th><th>creator</th><th>caption</th><th>likes</th>"
                      "<th>dims</th><th>dur</th><th>match</th><th>score</th><th>usable</th>"
                      "<th>verdict</th></tr>" + "".join(trs) + "</table>")
    return "\n".join(blocks)


def build_full_analysis(run_id):
    run_dir = BENCH_DIR / run_id
    rows = [r for r in _load_cache(run_id) if not r.get("error")]
    type_summary, cat_type_summary, cross_dup = _aggregate(rows)
    ranking = sorted(type_summary.items(), key=lambda kv: kv[1].get("final_yield", 0), reverse=True)
    tax_glob, tax_by_type = failure_taxonomy(rows)
    costs = cost_and_counts(rows)
    cat_rank = category_rankings(rows)
    avg_runtime = round(sum(float((r.get("metrics") or {}).get("average_search_time", 0)) for r in rows)
                        / max(1, len(rows)), 2)
    analysis = {
        "run_id": run_id, "queries_analysed": len(rows),
        "categories_covered": sorted({r.get("category") for r in rows if r.get("category")}),
        "query_type_ranking": [{"query_type": t, **s} for t, s in ranking],
        "category_rankings": cat_rank,
        "failure_taxonomy_global": tax_glob, "failure_taxonomy_by_query_type": tax_by_type,
        "cross_query_duplicate_rate": cross_dup, "average_runtime_per_query_s": avg_runtime,
        "counts_and_cost": costs,
        "relevance_recalibrated": relevance_recalibrated(rows),
        "recommendations": _derive_recommendations(rows, type_summary, cat_type_summary),
    }
    (run_dir / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")

    trs = "".join(
        f"<tr><td>{r['query_type']}</td><td>{r['n_queries']}</td><td>{r['relevant_at_10']}</td>"
        f"<td>{r['usable_at_10']}</td><td>{r['exact_match_rate']}</td><td>{r['final_yield']}</td>"
        f"<td>{r['caption_free_rate']}</td><td>{r['creator_diversity']}</td>"
        f"<td>{r['average_search_time']}s</td></tr>" for r in analysis["query_type_ranking"])
    cat_html = ""
    for c, rk in cat_rank.items():
        rows_html = "".join(f"<tr><td>{x['query_type']}</td><td>{x['n']}</td><td>{x['relevant_at_10']}</td>"
                            f"<td>{x['usable_at_10']}</td><td>{x['final_yield']}</td>"
                            f"<td>{x['caption_free_rate']}</td></tr>" for x in rk)
        cat_html += (f"<h3>{c}</h3><table><tr><th>query type</th><th>n</th><th>rel@10</th>"
                     f"<th>usable@10</th><th>yield</th><th>caption-free</th></tr>{rows_html}</table>")
    tax_html = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in tax_glob.items())
    html = f"""<!doctype html><meta charset=utf-8><title>TikTok Search Lab analysis - {run_id}</title>
<style>body{{font-family:system-ui,Segoe UI,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px}}
table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}}th,td{{border:1px solid #ccc;padding:5px 8px;text-align:right}}
th:first-child,td:first-child,td:nth-child(2),td:nth-child(3){{text-align:left}}th{{background:#f3f0e6}}
code{{background:#f3f0e6;padding:1px 5px;border-radius:4px}}h1,h2,h3{{font-family:inherit}}small{{color:#666;font-weight:400}}</style>
<h1>TikTok Search Lab &mdash; full analysis</h1>
<p>Run <code>{run_id}</code> &middot; {len(rows)} real queries &middot; categories: {', '.join(analysis['categories_covered'])}<br>
downloads {costs['downloads_ok']}/{costs['downloads_attempted']} ok &middot; segments {costs['segments_discovered']} &middot;
~{costs['vision_calls_est']} vision calls &middot; est. cost <b>${costs['vision_cost_usd_est']}</b>
(range ${costs['vision_cost_usd_range'][0]}&ndash;${costs['vision_cost_usd_range'][1]}) &middot;
avg {avg_runtime}s/query &middot; cross-query dup {cross_dup}</p>
<h2>Query-type ranking (overall, by final yield)</h2>
<table><tr><th>query type</th><th>n</th><th>rel@10</th><th>usable@10</th><th>exact</th><th>yield</th>
<th>caption-free</th><th>creator div.</th><th>search time</th></tr>{trs}</table>
<h2>Rankings per category</h2>{cat_html}
<h2>Failure taxonomy (global)</h2><table><tr><th>bucket</th><th>count</th></tr>{tax_html}</table>
<p><small>{costs['cost_note']}</small></p>
<h2>Per-query top-10 review (spot-check vision judgments)</h2>{top10_review_html(rows)}"""
    (run_dir / "analysis.html").write_text(html, encoding="utf-8")
    return analysis


if __name__ == "__main__":
    rid = sys.argv[1] if len(sys.argv) > 1 else None
    if not rid:
        runs = sorted(BENCH_DIR.glob("bench_*"))
        rid = runs[-1].name if runs else None
    if not rid:
        print("no run found"); raise SystemExit(1)
    a = build_full_analysis(rid)
    print(json.dumps({"run": a["run_id"], "queries": a["queries_analysed"],
                      "categories": a["categories_covered"],
                      "top_query_types": [r["query_type"] for r in a["query_type_ranking"][:3]],
                      "cost": a["counts_and_cost"]}, ensure_ascii=False, indent=2))
