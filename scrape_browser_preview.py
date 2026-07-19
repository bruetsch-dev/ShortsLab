"""Thread-safe, read-only live preview of the headed TikTok/X scraper browser."""
import threading
import time

_LOCK = threading.Lock()
_THREAD = threading.local()
_STATE = {"jpeg": b"", "version": 0, "captured_at": 0.0, "platform": "",
          "query": "", "sort": "", "owner_slug": "", "accepted_version": 0,
          "last_accepted_path": "", "last_accepted_platform": "",
          "last_accepted_query": "", "last_accepted_clip_id": "",
          "last_accepted_at": 0.0, "last_accepted_start": 0.0,
          "last_accepted_poster": ""}


def set_owner(project_slug=""):
    """Associate screenshots produced by this scraper thread with one project.

    Scrape searches run in worker threads.  Keeping the owner thread-local prevents a
    simultaneous longform/other job from accidentally claiming the globally newest browser
    image in its own processing screen.
    """
    _THREAD.owner_slug = str(project_slug or "").strip()


def _owner():
    return str(getattr(_THREAD, "owner_slug", "") or "").strip()


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
                      query=str(query or ""), sort=str(sort or ""), owner_slug=_owner())
    return True


def snapshot():
    with _LOCK:
        return dict(_STATE)


def status():
    with _LOCK:
        return {key: value for key, value in _STATE.items() if key != "jpeg"} | {
            "available": bool(_STATE["jpeg"])}


def mark_accepted(path, platform="", query="", clip_id="", start=0.0, poster=""):
    """Publish the latest clip that passed semantic matching for at least one scene.

    `poster` is a browser-playable JPEG frame of the clip: the raw proxy is often HEVC/H.265,
    which Chromium/WebView2 refuse to render, so the preview shows the poster (always renders)
    with the video only as a best-effort enhancement."""
    with _LOCK:
        _STATE.update(
            owner_slug=_owner(),
            accepted_version=int(_STATE["accepted_version"]) + 1,
            last_accepted_path=str(path or ""),
            last_accepted_platform=str(platform or ""),
            last_accepted_query=str(query or ""),
            last_accepted_clip_id=str(clip_id or ""),
            last_accepted_at=time.time(),
            last_accepted_start=max(0.0, float(start or 0.0)),
            last_accepted_poster=str(poster or ""),
        )


def clear():
    with _LOCK:
        _STATE.update(jpeg=b"", version=int(_STATE["version"]) + 1,
                      captured_at=0.0, platform="", query="", sort="", owner_slug="",
                      accepted_version=int(_STATE["accepted_version"]) + 1,
                      last_accepted_path="", last_accepted_platform="",
                      last_accepted_query="", last_accepted_clip_id="",
                      last_accepted_at=0.0, last_accepted_start=0.0,
                      last_accepted_poster="")
