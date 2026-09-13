"""Audits the direct media URL returned by Bright Data without a browser."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import brightdata_tiktok


def main() -> int:
    import requests
    query = "Japan vending machine hot canned coffee"
    outcome = {"query": query, "bright_only": True}
    try:
        grouped = brightdata_tiktok.search_many([query], per_query=1, timeout_s=240, max_records=1)
        items = grouped.get(query) or []
        outcome["records"] = len(items)
        if not items:
            raise RuntimeError("Bright returned no record")
        item = items[0]
        url = str(item.get("_media_url") or "")
        outcome["media_host"] = url.split("/")[2] if "://" in url else ""
        response = requests.get(url, stream=True, timeout=(20, 60), headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
            "Referer": "https://www.tiktok.com/", "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        outcome.update({"status": response.status_code, "content_type": response.headers.get("content-type", ""),
                        "content_length": response.headers.get("content-length", ""),
                        "first_bytes": response.raw.read(24).hex()})
        outcome["ok"] = response.status_code == 200 and "html" not in outcome["content_type"].lower()
    except Exception as exc:  # preserve evidence even if Bright times out
        outcome.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    out = ROOT / "projects" / "v4_bright_direct_media_audit.json"
    out.write_text(json.dumps(outcome, indent=2), encoding="utf-8")
    print(json.dumps(outcome, indent=2), flush=True)
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
