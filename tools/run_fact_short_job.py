"""Start one Clip Short through the same app job path and stream its progress."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app


def main():
    # PowerShell may launch Python with cp1252 even though the app legitimately logs Japanese
    # search terms. Monitoring a healthy scrape must never terminate its owning process merely
    # because the console cannot render those characters.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--script-file", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--base-project", default="many_japanese_couples_treat_christmas_eve_like")
    args = parser.parse_args()
    base = ROOT / "projects" / args.base_project / "input" / "run_form.json"
    fields = json.loads(base.read_text(encoding="utf-8")) if base.exists() else {}
    fields.update({
        "title": args.title,
        "script": Path(args.script_file).read_text(encoding="utf-8").strip(),
        "loaded_project_source": "",
        "slug": "",
        "run_type": "normal",
        "clip_short_format": "standard",
        "clip_source": "scrape",
        "scraping_engine": "v3",
        "scrape_platforms": "tiktok,x,instagram",
        "scrape_sort": "ALL",
        "reasoning_model": "google/gemini-3.5-flash-lite",
        "reasoning_mode": "medium",
        "influencer_hook": "",
        "halt_after_speech": "",
        "background_music_choice": "none",
        "background_music_enabled": "",
        "out_background_music": "",
    })
    for stale in ("hook_text", "impact_word", "hook_keywords"):
        fields[stale] = "" if stale != "hook_keywords" else "[]"
    job_id = app.start_job(fields, {})
    print(f"JOB_ID|{job_id}", flush=True)
    seen = 0
    while True:
        with app.JOB_LOCK:
            job = dict(app.JOBS[job_id])
            logs = list(job.get("logs") or [])
        for line in logs[seen:]:
            print(str(line), flush=True)
        seen = len(logs)
        if job.get("status") != "running":
            print("FINAL_STATUS|" + str(job.get("status")), flush=True)
            if job.get("result") is not None:
                print("RESULT|" + json.dumps(job["result"], ensure_ascii=False, default=str), flush=True)
            if job.get("error"):
                print("ERROR|" + str(job["error"]), flush=True)
            return 0 if job.get("status") == "done" else 1
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
