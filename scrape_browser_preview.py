"""Thread-safe, read-only live preview of the headed TikTok/X scraper browser."""
import threading
import time

_LOCK = threading.Lock()
_STATE = {"jpeg": b"", "version": 0, "captured_at": 0.0, "platform": "",
          "query": "", "sort": ""}


def capture(page, platform, query="", sort="", force=False):
    """Capture on Playwright's owning thread, at most once every three seconds globally."""
    now = time.monotonic()
    with _LOCK:
        if not force and now - float(_STATE["captured_at"] or 0) < 1.5:
            return False
    try:
        jpeg = page.screenshot(type="jpeg", quality=58, full_page=False)
    except Exception:
        return False
    with _LOCK:
        _STATE.update(jpeg=jpeg, version=int(_STATE["version"]) + 1,
                      captured_at=now, platform=str(platform or ""),
                      query=str(query or ""), sort=str(sort or ""))
    return True


def snapshot():
    with _LOCK:
        return dict(_STATE)


def status():
    with _LOCK:
        return {key: value for key, value in _STATE.items() if key != "jpeg"} | {
            "available": bool(_STATE["jpeg"])}


def clear():
    with _LOCK:
        _STATE.update(jpeg=b"", version=int(_STATE["version"]) + 1,
                      captured_at=0.0, platform="", query="", sort="")
