"""One-record Bright V4 media smoke test.

This deliberately validates the full new contract before a costly production run:
Bright Dataset discovery -> Bright Unlocker -> real MP4 bytes.  It does not use
any browser resolver or legacy scraper.
"""
from __future__ import annotations

import json
import sys
import tempfile
from urllib.parse import urlparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import brightdata_tiktok as bright
import scrape_v4


QUERY = "Japanese students classroom cleaning"
ZONE = "web_unlocker1"
REPORT = Path("projects") / "v4_bright_media_smoke.json"


def main() -> int:
    log: list[str] = []
    try:
        grouped = bright.search_many([QUERY], per_query=3, timeout_s=600,
                                     max_records=3, status_cb=log.append)
        items = grouped.get(QUERY) or []
        result: dict[str, object] = {"query": QUERY, "log": log, "records": len(items)}
        if not items:
            result["ok"] = False
            result["reason"] = "Bright returned no usable record"
        else:
            item = items[0]
            target = Path(tempfile.gettempdir()) / "shortslab_bright_v4_smoke.mp4"
            target.unlink(missing_ok=True)
            ok = scrape_v4._download_bright_media(item, target, ZONE, status_cb=log.append)
            result.update({
                "ok": ok,
                "id": str(item.get("id") or ""),
                "has_media_url": bool(item.get("_media_url")),
                "bytes": target.stat().st_size if target.exists() else 0,
                "path": str(target) if target.exists() else "",
                "media_host": urlparse(str(item.get("_media_url") or "")).netloc,
            })
            if target.exists():
                result["header"] = target.read_bytes()[:16].hex()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 2
    except Exception as exc:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({"ok": False, "log": log,
                                      "error": f"{type(exc).__name__}: {exc}"}, indent=2),
                          encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
