"""Direct local TikTok collection for one no-repeat manual Short.

Search terms are deliberately action-led.  This is not an app pipeline and it
does not call a paid scraping/search API: it uses the already authenticated
TikTok session to retrieve fresh page results and capture the browser's own
video response.  The output is only a candidate pool; Luna reviews it later.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tiktok_login

SLUG = "manual_school_cleaning_no_repeats"
TERMS = [
    "日本 高校生 掃除 教室",       # active in-class cleaning, not generic school posts
    "学校 雑巾がけ レース 生徒",   # energetic floor-cleaning action for the hook/pacing
    "高校生 廊下 モップ 掃除",     # hallway mop action
    "中学生 清掃活動 学校",        # group school-cleaning footage
    "高校生 掃除当番 教室",        # normal cleaning-duty context
    "学校 ほうき 掃除 生徒",       # visible broom action
    "日本 学校 清掃時間 生徒",     # common school clean-up period, documentary-style footage
    "日本 中学校 教室 掃除 生徒",  # actual classrooms rather than cleaning-product demos
    "高校 学校 清掃 廊下 生徒",    # moving hallway action
    "日本 学生 机 拭く 教室",     # desks / cloth wiping for the shared-space beat
    "日本 学校 掃除 みんなで",    # group participation, avoids solo product clips
    "高校生 学校 清掃 ほうき",    # broom motion without broad household-cleaning query
]


def _prior_ids() -> set[str]:
    ids: set[str] = set()
    for project in (ROOT / "projects").glob("manual_*school*clean*"):
        if project.name == SLUG:
            continue
        for audit_name in ("source_audit.json", "source_window_audit.json"):
            audit = project / "review" / audit_name
            if not audit.exists():
                continue
            try:
                for row in json.loads(audit.read_text(encoding="utf-8")):
                    ids.add(str(row.get("source_id") or ""))
            except Exception:
                pass
    return ids


def main() -> int:
    project = ROOT / "projects" / SLUG
    target = project / "seedance 2.0" / "_fresh_sources"
    review = project / "review"
    target.mkdir(parents=True, exist_ok=True)
    review.mkdir(parents=True, exist_ok=True)
    if not tiktok_login.ensure_session(status_cb=print, timeout_s=120):
        raise RuntimeError("TikTok session is not ready; no fallback was used.")

    seen = _prior_ids()
    audit_path = review / "fresh_source_audit.json"
    try:
        accepted = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else []
    except Exception:
        accepted = []
    # Keep every already downloaded candidate; the reviewer, not the collector,
    # decides whether it belongs in the edit.  This makes widening the pool safe.
    seen.update(str(row.get("source_id") or "") for row in accepted)
    rejected = []
    for term in TERMS:
        print(f"\nFRESH LOCAL SEARCH: {term}", flush=True)
        items = tiktok_login.search_sync(term, want=6, status_cb=print, sort="RELEVANCE", timeout_s=90) or []
        for item in items:
            source_id = str(item.get("id") or item.get("videoId") or "").strip()
            url = str(item.get("webVideoUrl") or item.get("url") or "").strip()
            if not source_id or not url or source_id in seen:
                continue
            seen.add(source_id)
            dest = target / f"tiktok_{source_id}.mp4"
            got = tiktok_login.download_sync(url, dest, status_cb=print, timeout_s=75)
            if not got or not dest.is_file() or dest.stat().st_size < 80_000:
                rejected.append({"source_id": source_id, "query": term, "reason": "download did not produce a complete MP4"})
                dest.unlink(missing_ok=True)
                continue
            accepted.append({
                "source_id": source_id, "query": term, "url": url,
                "path": str(dest), "caption": str(item.get("desc") or "")[:400],
            })
            print(f"  saved fresh source {source_id} ({dest.stat().st_size // 1024} KiB)", flush=True)
            # Two new videos per intent are enough before visual review; do not download a
            # giant uninspected pile merely because TikTok returns it.
            if sum(1 for row in accepted if row["query"] == term) >= 2:
                break
    audit_path.write_text(json.dumps(accepted, ensure_ascii=False, indent=2), encoding="utf-8")
    (review / "fresh_source_download_rejections.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")
    (project / "input" / "manual_search_terms.json").write_text(json.dumps(TERMS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DONE: {len(accepted)} fresh downloadable sources; {len(rejected)} failed downloads.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
