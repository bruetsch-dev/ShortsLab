"""Score a finished clip-short run the same way every time.

Reading a run by eye is how the download starvation survived several sessions: the log showed
searching and ranking throughout, and the one number that mattered - downloads against ranked
candidates - was never lined up next to the clock.  This prints the funnel first, then the
per-beat scorecard, then the checks that have each cost a run before.

  python tools/eval_run.py <project-slug-or-dir>
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import agent_core


def _project_dir(arg):
    from pathlib import Path
    p = Path(arg)
    if p.is_dir():
        return p
    p = agent_core.PROJECTS_DIR / arg
    if p.is_dir():
        return p
    raise SystemExit(f"no such project: {arg}")


def main(arg):
    root = _project_dir(arg)
    report_path = root / "review" / "scrape_v2_report.json"
    if not report_path.is_file():
        raise SystemExit(f"no scrape report at {report_path}")
    r = json.loads(report_path.read_text(encoding="utf-8"))
    scenes = r.get("scenes") or []
    budgets = r.get("budgets") or {}

    print(f"=== {root.name}")
    print("--- funnel")
    ranked = r.get("metadata_candidates", 0)
    downloads = r.get("downloaded_sources", 0)
    time_s = float(budgets.get("time_s") or 0)
    for label, value in (
            ("queries executed", r.get("queries_executed", 0)),
            ("raw results", r.get("raw_results", 0)),
            ("ranked candidates", ranked),
            ("downloads", downloads),
            ("segments discovered", r.get("segments_discovered", 0)),
            ("segments matched", r.get("segments_semantic_passed", 0)),
            ("time used (s)", int(time_s)),
    ):
        print(f"  {label:22s} {value}")

    print("--- beats")
    kinds = Counter()
    for s in scenes:
        kind = str(s.get("assignment_type") or "?")
        kinds[kind] += 1
        mark = {"exact": "OK ", "exact_match": "OK ",
                "alternative": "alt", "alternative_match": "alt",
                "context_fallback": "ctx", "borrowed_clip": "BOR",
                "still_fallback": "STL"}.get(kind, "?  ")
        text = str(s.get("scene_text") or "")[:52]
        why = str(s.get("uncovered_reason") or "")
        tail = f"   <- {why[:58]}" if why else ""
        print(f"  {mark} {s.get('scene_id'):>2}  {text}{tail}")
    print("  " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))

    best = ((r.get("matcher") or {}).get("best_by_scene") or {})
    if best:
        scores = sorted((float(v.get("best_overall") or 0) for v in best.values()), reverse=True)
        strong = [v for v in scores if v >= 9.0]
        ok = [v for v in scores if v >= 7.0]
        mid = scores[len(scores) // 2] if scores else 0.0
        print(f"--- match scores  best={scores[0]:.2f} median={mid:.2f} "
              f">=7: {len(ok)}/{len(scores)}  >=9: {len(strong)}")

    rej = r.get("rejections") or {}
    if rej:
        total_rej = sum(int(v) for v in rej.values() if isinstance(v, (int, float)))
        print(f"--- rejections ({total_rej} total)")
        for reason, count in sorted(rej.items(), key=lambda kv: -kv[1]):
            share = f"  ({count / downloads:.0%} of downloads)" if downloads else ""
            print(f"  {reason:38s} {count}{share}")

    print("--- checks that have cost a run before")
    problems = []
    if ranked and downloads and downloads < ranked * 0.02:
        problems.append(f"downloads ({downloads}) are under 2% of ranked candidates ({ranked}) - "
                        f"look at the download budget before blaming the matcher")
    if time_s and budgets.get("queries") and time_s < 0.6 * 7200 and kinds.get("borrowed_clip"):
        problems.append(f"stopped after {int(time_s)}s with beats still borrowed - it ran out of "
                        f"things it believed it was allowed to do, not out of clock")
    dupes = Counter()
    for s in scenes:
        if s.get("borrowed_from_scene") is not None:
            dupes[s["borrowed_from_scene"]] += 1
    repeated = {k: v for k, v in dupes.items() if v > 1}
    if repeated:
        # Say WHICH failure this is. The borrow path already spreads by least-used donor, so
        # repeats with few donors are the starved pool showing through, not a borrow bug -
        # chasing the borrow logic there fixes nothing.
        donors = sum(1 for s in scenes
                     if str(s.get("assignment_type") or "") not in
                     ("borrowed_clip", "still_fallback", "uncovered_still"))
        borrowers = kinds.get("borrowed_clip", 0)
        why = (f"{borrowers} beats had to share {donors} donor(s) - the pool is the problem"
               if donors and borrowers > donors else "the borrow spread is the problem")
        problems.append(f"one clip is borrowed onto several beats: {repeated} ({why})")
    for p in problems:
        print(f"  ! {p}")
    if not problems:
        print("  none")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
