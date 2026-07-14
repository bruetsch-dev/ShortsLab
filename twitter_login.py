"""X/Twitter login-based clip discovery - the second scrape backend next to TikTok.

Same pattern as tiktok_login.py: the user logs in to x.com ONCE inside a Chromium window
we control (a dedicated persistent profile, `twitter-profile/`), and from then on we drive
that logged-in session to run real VIDEO searches (x.com/search?f=video) and collect tweet
objects. Each tweet with a native video is normalized into the exact item schema
`clip_scraper._item_meta` already understands (`_platform: "twitter"`), so the whole
existing quality pipeline (metadata pre-filter -> yt-dlp download -> gates -> OCR ->
vision matcher) is reused unchanged.

THREADING: unlike the TikTok session (which lives on the scrape thread), ALL Playwright
work here is funneled through ONE dedicated worker thread (`_EXECUTOR`). That makes
`search_async()` safely awaitable from the scrape thread, so clip_scraper can search
TikTok and X at the SAME time without cross-thread Playwright errors.

Public surface used by clip_scraper / app:
    available() / is_ready() / status()
    login(status_cb, timeout_s)          -> headed one-time login (blocking)
    search_async(query, want, status_cb) -> concurrent.futures.Future -> [item, ...]
    search_stats() / reset_search_stats()
    export_cookies_txt(path)             -> cookies.txt for yt-dlp (x.com domains)
    close_session()
"""

import concurrent.futures
import json
import os
import re
import subprocess
import threading
import scrape_browser_preview
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except Exception:                       # playwright not installed
    sync_playwright = None

import tiktok_login as _tt              # reuse the off-screen taskbar hider (same parking band)

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path(os.environ.get("TWITTER_PROFILE_DIR") or (ROOT / "twitter-profile"))
_STATE_DIR = ROOT / "generated_assets" / "twitter"
COOKIES_TXT = _STATE_DIR / "twitter_cookies.txt"
_MARKER = _STATE_DIR / "logged_in.json"

# cookies that only exist for a logged-in X session
SESSION_COOKIE_NAMES = ("auth_token",)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_LOCALE = os.environ.get("TWITTER_LOCALE", "ja-JP").strip() or "ja-JP"

_SEARCH_STATS = {"searches": 0, "items": 0, "login_wall": 0, "captcha": 0,
                 "timeline_responses": 0, "health_checks": 0, "unavailable": 0,
                 "nsfw_skipped": 0}
_EXECUTOR = None
_EXEC_LOCK = threading.Lock()
_SESSION = [None]                       # only ever touched from the executor thread

# PRE-DOWNLOAD NSFW GATE: X carries plenty of adult content. Items are dropped at METADATA
# time (never even downloaded) when X's own `possibly_sensitive` flag is set OR the
# text/hashtags/handle hit obvious adult terms. (Mirrored in instagram_login.py - keep in sync.)
NSFW_RE = re.compile(
    r"(?i)\b(porn|nsfw|xxx|onlyfans|fansly|hentai|nudes?|lewd|bdsm|fetish|camgirl|escort|"
    r"stripper|milf)\b|18\s*[+＋]|18plus|エロ|裏垢|無修正|アダルト|オフパコ|セフレ|av女優|"
    r"風俗|デリヘル|パパ活|えっち|性感")


def search_stats():
    return dict(_SEARCH_STATS)


def reset_search_stats():
    _SEARCH_STATS.update(searches=0, items=0, login_wall=0, captcha=0,
                         timeline_responses=0, health_checks=0, unavailable=0,
                         nsfw_skipped=0)


def _status(cb, msg):
    if cb:
        try:
            cb(msg)
        except Exception:
            pass
    else:
        print(msg)


def available():
    return sync_playwright is not None


def _has_session_cookie(cookies):
    names = {str(c.get("name") or "") for c in (cookies or [])
             if "twitter" in str(c.get("domain") or "").lower()
             or "x.com" in str(c.get("domain") or "").lower()}
    return any(n in names for n in SESSION_COOKIE_NAMES)


def is_ready():
    return bool(available() and _MARKER.exists() and PROFILE_DIR.exists())


def status():
    info = {"available": available(), "ready": is_ready(),
            "profile": str(PROFILE_DIR), "logged_in_at": None}
    try:
        if _MARKER.exists():
            info["logged_in_at"] = json.loads(_MARKER.read_text("utf-8")).get("at")
    except Exception:
        pass
    return info


def _write_marker():
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _MARKER.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S")}), "utf-8")


def _cookies_to_netscape(cookies):
    lines = ["# Netscape HTTP Cookie File", "# Written by twitter_login.py", ""]
    for c in cookies or []:
        domain = str(c.get("domain") or "")
        dl = domain.lower()
        if "twitter" not in dl and "x.com" not in dl:
            continue
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        path = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = c.get("expires")
        try:
            expires = str(int(expires)) if expires and float(expires) > 0 else "0"
        except (TypeError, ValueError):
            expires = "0"
        lines.append("\t".join([domain, "TRUE", path, secure, expires,
                                str(c.get("name") or ""), str(c.get("value") or "")]))
    return "\n".join(lines) + "\n"


def export_cookies_txt(path=None, cookies=None):
    path = Path(path or COOKIES_TXT)
    if cookies is None:
        # sess.cookies() is a Playwright call - it MUST run on the worker thread, or it
        # touches the sync-Playwright context from the wrong thread and the whole process
        # dies with greenlet "Cannot switch to a different thread". _merge_backend_cookies
        # calls this from the TikTok/main thread, so route it through the executor.
        cookies = _session_cookies_threadsafe()
        if cookies is None:
            return str(path) if path.exists() else None
    if not _has_session_cookie(cookies):
        return str(path) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_cookies_to_netscape(cookies), "utf-8")
    return str(path)


def _kill_stale_profile_processes():
    """Kill any Chromium zombie still holding twitter-profile (same failure mode as TikTok:
    the next launch delegates to the zombie and dies with TargetClosedError)."""
    if os.name != "nt":
        return
    try:
        marker = str(PROFILE_DIR).replace("'", "''")
        cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" -ErrorAction Stop | "
            f"Where-Object {{ $_.CommandLine -and $_.CommandLine -like '*{marker}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


# --------------------------------------------------------------------------- login

def login(status_cb=None, timeout_s=300):
    """Open a real Chromium window, let the user sign in to X, persist the session. Blocking."""
    if not available():
        _status(status_cb, "X login: Playwright is not installed (pip install playwright "
                           "&& playwright install chromium).")
        return False
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _kill_stale_profile_processes()
    _status(status_cb, "Opening a Chromium window - log in to your X/Twitter account in it. "
                       "You only do this once.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, user_agent=_UA, locale=_LOCALE,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-default-browser-check", "--window-position=160,80"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _tt._place_window_visible(ctx, 160, 80)   # force ON-SCREEN even if a scrape saved it off-screen
            try:
                page.goto("https://x.com/login", timeout=60000)
            except Exception:
                try:
                    page.goto("https://x.com/", timeout=60000)
                except Exception:
                    pass
            deadline = time.time() + max(30, int(timeout_s))
            ok = False
            while time.time() < deadline:
                try:
                    if _has_session_cookie(ctx.cookies()):
                        ok = True
                        break
                except Exception:
                    pass
                if not ctx.pages:
                    break
                time.sleep(2.0)
            if ok:
                export_cookies_txt(cookies=ctx.cookies())
                _write_marker()
                _status(status_cb, "X login captured - the session is saved. Future scrapes "
                                   "search X alongside TikTok automatically.")
            else:
                _status(status_cb, "X login timed out / window closed before sign-in completed.")
            return ok
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def logout():
    import shutil
    close_session()
    for target in (PROFILE_DIR, _MARKER, COOKIES_TXT):
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        except Exception:
            pass
    return True


# --------------------------------------------------------------------------- search

def _extract_video_tweets(payload):
    """Recursively pull tweets that carry a NATIVE VIDEO out of a SearchTimeline GraphQL
    response (schema drifts, so we walk the tree instead of hard-coding paths)."""
    found = []

    def _screen_name(node):
        # user lives under core.user_results.result: legacy.screen_name (old) or core.screen_name (new)
        for path in (("core", "user_results", "result", "legacy", "screen_name"),
                     ("core", "user_results", "result", "core", "screen_name")):
            cur = node
            for key in path:
                cur = cur.get(key) if isinstance(cur, dict) else None
            if cur:
                return str(cur), node.get("core", {})
        return "", {}

    def _walk(node):
        if isinstance(node, dict):
            legacy = node.get("legacy")
            if isinstance(legacy, dict) and legacy.get("id_str"):
                ee = legacy.get("extended_entities")
                media = (ee or {}).get("media") if isinstance(ee, dict) else None
                vids = [m for m in (media or [])
                        if isinstance(m, dict) and m.get("type") == "video"]
                if vids:
                    found.append((node, legacy, vids[0]))
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(payload)
    items = []
    for node, legacy, vid in found:
        sn, _core = _screen_name(node)
        tid = str(legacy.get("id_str"))
        oi = vid.get("original_info") if isinstance(vid.get("original_info"), dict) else {}
        vi = vid.get("video_info") if isinstance(vid.get("video_info"), dict) else {}
        tags = [{"name": str(h.get("text") or "")}
                for h in ((legacy.get("entities") or {}).get("hashtags") or [])
                if isinstance(h, dict) and h.get("text")]
        # NSFW gate BEFORE the item ever reaches ranking/download
        blob = " ".join([str(legacy.get("full_text") or ""), sn]
                        + [t["name"] for t in tags])
        if legacy.get("possibly_sensitive") or NSFW_RE.search(blob):
            _SEARCH_STATS["nsfw_skipped"] += 1
            continue
        try:
            likes = int(legacy.get("favorite_count") or 0)
        except (TypeError, ValueError):
            likes = 0
        views_node = node.get("views") if isinstance(node.get("views"), dict) else {}
        try:
            views = int(views_node.get("count") or legacy.get("view_count") or 0)
        except (TypeError, ValueError):
            views = 0
        items.append({
            "id": tid,
            "desc": str(legacy.get("full_text") or ""),
            "webVideoUrl": f"https://x.com/{sn or 'i'}/status/{tid}",
            "_source": "twitter_login",       # backend_download -> yt-dlp
            "_platform": "twitter",           # scaled like-gate + reporting
            "video": {"width": int(oi.get("width") or 0), "height": int(oi.get("height") or 0),
                      "duration": int(vi.get("duration_millis") or 0)},
            "stats": {"diggCount": likes, "playCount": views},
            "createTime": int(tid) if tid.isdigit() else 0,
            "author": {"uniqueId": sn, "nickname": sn},
            "hashtags": tags,
        })
    return items


class Session:
    """A logged-in X browser session. MUST only be used from the executor thread."""

    def __init__(self, status_cb=None):
        self._p = None
        self._ctx = None
        self._status_cb = status_cb
        self._pids = set()
        self._hide_stop = threading.Event()
        self._watcher = None
        self._open()

    def _open(self):
        _kill_stale_profile_processes()
        self._p = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--no-first-run",
                "--no-default-browser-check", "--mute-audio"]
        visible = os.environ.get("TWITTER_WINDOW_VISIBLE", "").strip().lower() in ("1", "true", "yes")
        if not visible:
            # same off-screen trick as TikTok: headed (X also degrades headless sessions) but
            # parked far off-screen with anti-throttle flags; taskbar button stripped below.
            args += ["--window-position=-2400,-2400", "--window-size=1280,900",
                     "--disable-backgrounding-occluded-windows",
                     "--disable-renderer-backgrounding",
                     "--disable-background-timer-throttling",
                     "--disable-features=CalculateNativeWinOcclusion"]
        self._ctx = self._p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, user_agent=_UA, locale=_LOCALE,
            viewport={"width": 1280, "height": 900}, args=args)
        if not visible:
            # CDP-force off-screen (persistent profile may restore an on-screen position), then hide.
            _tt._park_window_offscreen(self._ctx, "TWITTER_WINDOW_VISIBLE")
            for _wait in (0.4, 1.2, 2.0):
                time.sleep(_wait)
                if _tt._hide_offscreen_from_taskbar():
                    break
            # ROBUST, CDP-independent enforcement by browser PID + a watcher for the whole session
            # (this is the real fix for windows that kept showing in the taskbar - see tiktok_login).
            for _ in range(6):
                self._pids = _tt._profile_pids(PROFILE_DIR)
                if self._pids:
                    break
                time.sleep(0.5)
            if self._pids:
                _tt._enforce_offscreen_hidden(self._pids)
                self._watcher = _tt._spawn_taskbar_watcher(self._pids, self._hide_stop)

    def cookies(self):
        try:
            return self._ctx.cookies()
        except Exception:
            return []

    def search(self, query, want=12, status_cb=None, max_scrolls=12, timeout_s=None,
               sort="MOST_LIKED"):
        cb = status_cb or self._status_cb
        query = str(query or "").strip()
        if not query:
            return []
        deadline = (time.monotonic() + max(0.1, float(timeout_s))
                    if timeout_s is not None else None)
        _tt._hide_offscreen_from_taskbar()
        collected, seen = [], set()
        timeline_seen = [False]

        def _absorb(payload):
            for it in _extract_video_tweets(payload):
                if it["id"] in seen:
                    continue
                seen.add(it["id"])
                collected.append(it)

        page = self._ctx.new_page()

        def _on_response(resp):
            try:
                if "SearchTimeline" in resp.url or "/search/adaptive" in resp.url:
                    if not timeline_seen[0]:
                        timeline_seen[0] = True
                        _SEARCH_STATS["timeline_responses"] += 1
                    _absorb(resp.json())
            except Exception:
                pass

        page.on("response", _on_response)
        try:
            from urllib.parse import quote
            # f=video is DEAD on today's X (no Videos tab anymore; it silently falls back to
            # "Top" = mostly photos/text, which made every X search look video-less). f=media
            # is the only remaining filter tab; the XHR extraction keeps only native videos.
            url = f"https://x.com/search?q={quote(query, safe='')}&src=typed_query&f=media"
            try:
                nav_ms = 45000 if deadline is None else max(
                    1000, min(45000, int((deadline - time.monotonic()) * 1000)))
                page.goto(url, timeout=nav_ms, wait_until="domcontentloaded")
                scrape_browser_preview.capture(page, "X", query, sort)
            except Exception as exc:
                _status(cb, f"X search: navigation failed for {query!r} ({exc.__class__.__name__}).")
            page.wait_for_timeout(2200)
            # X intermittently renders its generic client-side "Something went wrong" shell
            # instead of firing SearchTimeline. Detect that state visible in the live scraper and
            # recover once; previously we merely scrolled the broken page for several seconds.
            try:
                page_text = (page.locator("body").inner_text(timeout=2500) or "").casefold()
            except Exception:
                page_text = ""
            if any(marker in page_text for marker in (
                    "something went wrong", "try reloading", "don’t fret", "don't fret")):
                _status(cb, f"X search: transient error page for {query!r}; reloading once.")
                try:
                    page.reload(timeout=min(30000, nav_ms), wait_until="domcontentloaded")
                    page.wait_for_timeout(2200)
                except Exception as exc:
                    _status(cb, f"X search: recovery reload failed for {query!r} "
                                f"({exc.__class__.__name__}).")
            scrape_browser_preview.capture(page, "X", query, sort, force=True)
            scrolls, stagnant, last_n = 0, 0, len(collected)
            while (len(collected) < want and scrolls < max_scrolls and stagnant < 3
                   and (deadline is None or time.monotonic() < deadline)):
                page.mouse.wheel(0, 2600)
                page.wait_for_timeout(1100)
                scrape_browser_preview.capture(page, "X", query, sort, force=True)
                scrolls += 1
                if len(collected) <= last_n:
                    stagnant += 1
                    # X's SearchTimeline XHR often lands only after 2-3 scrolls - giving up on
                    # the first empty scroll made every search look at "just the first result".
                    if not collected and scrolls >= 3:
                        break
                else:
                    stagnant = 0
                last_n = len(collected)
            if not collected:
                try:
                    low = (page.content() or "").lower()
                    if "log in" in low and "sign up" in low:
                        _SEARCH_STATS["login_wall"] += 1
                    if "captcha" in low or "arkose" in low or "verify your identity" in low:
                        _SEARCH_STATS["captcha"] += 1
                except Exception:
                    pass
                # Media tab can be consent/verify-gated (empty column) while "Top" still
                # renders - fall back once so X keeps contributing its few native videos.
                if deadline is None or time.monotonic() < deadline:
                    try:
                        top_url = f"https://x.com/search?q={quote(query, safe='')}&src=typed_query"
                        nav_ms = 30000 if deadline is None else max(
                            1000, min(30000, int((deadline - time.monotonic()) * 1000)))
                        page.goto(top_url, timeout=nav_ms, wait_until="domcontentloaded")
                        page.wait_for_timeout(2200)
                        scrape_browser_preview.capture(page, "X", query, sort, force=True)
                        for _ in range(3):
                            if collected or (deadline and time.monotonic() >= deadline):
                                break
                            page.mouse.wheel(0, 2600)
                            page.wait_for_timeout(1100)
                        if collected:
                            _status(cb, f"X search: media tab empty for {query!r} - "
                                        f"Top fallback found {len(collected)} video(s).")
                    except Exception:
                        pass
        finally:
            try:
                page.close()
            except Exception:
                pass
        _SEARCH_STATS["searches"] += 1
        _SEARCH_STATS["items"] += len(collected)
        if collected:
            try:
                export_cookies_txt(cookies=self.cookies())
            except Exception:
                pass
        sort_mode = str(sort or "MOST_LIKED").upper()
        metric = "diggCount" if sort_mode == "MOST_LIKED" else "playCount"
        if sort_mode in {"MOST_LIKED", "MOST_VIEWED"}:
            collected.sort(key=lambda item: int((item.get("stats") or {}).get(metric) or 0),
                           reverse=True)
        elif sort_mode == "MOST_RECENT":
            collected.sort(key=lambda item: int(item.get("createTime") or item.get("id") or 0),
                           reverse=True)
        return collected[:max(0, int(want))]

    def close(self):
        try:
            self._hide_stop.set()           # stop the taskbar-hide watcher
        except Exception:
            pass
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._p:
                self._p.stop()
        except Exception:
            pass
        self._ctx = self._p = None


# ------------------------------------------------------------------ executor plumbing

def _executor():
    global _EXECUTOR
    with _EXEC_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="xscrape")
        return _EXECUTOR


def _on_worker_thread():
    return threading.current_thread().name.startswith("xscrape")


def _session_cookies_threadsafe():
    """Read the session cookies ON the worker thread (None when no session). Any Playwright
    call from another thread crashes the process with a greenlet cross-thread error."""
    if _on_worker_thread():                 # already there - a future would deadlock
        return _SESSION[0].cookies() if _SESSION[0] is not None else None
    def _get():
        return _SESSION[0].cookies() if _SESSION[0] is not None else None
    try:
        return _executor().submit(_get).result(timeout=30)
    except Exception:
        return None


def _session_on_worker(status_cb=None):
    if _SESSION[0] is None:
        try:
            _SESSION[0] = Session(status_cb=status_cb)
        except Exception as exc:  # noqa: BLE001
            _status(status_cb, f"X session failed to open ({exc.__class__.__name__}: {exc}).")
            _SESSION[0] = None
    return _SESSION[0]


def _search_on_worker(query, want, status_cb, timeout_s=None, sort="MOST_LIKED"):
    sess = _session_on_worker(status_cb)
    if sess is None:
        return []
    try:
        return sess.search(query, want=want, status_cb=status_cb, timeout_s=timeout_s,
                           sort=sort) or []
    except Exception as exc:  # noqa: BLE001
        _status(status_cb, f"X search failed ({exc.__class__.__name__}: {exc}).")
        return []


def search_async(query, want=12, status_cb=None, timeout_s=None, sort="MOST_LIKED"):
    """Run a logged-in X video search on the dedicated worker thread. Returns a Future
    resolving to a list of normalized items; resolves to [] when not logged in."""
    if not is_ready():
        f = concurrent.futures.Future()
        f.set_result([])
        return f
    return _executor().submit(_search_on_worker, query, int(want), status_cb, timeout_s, sort)


def health_check(timeout_s=45):
    """API-free session/search-timeline check using a known broad video query."""
    _SEARCH_STATS["health_checks"] += 1
    before = search_stats()
    try:
        items = search_async("Japan viral video", want=3, status_cb=lambda _m: None,
                             timeout_s=timeout_s, sort="MOST_RECENT").result(
                                 timeout=max(5, timeout_s + 5)) or []
    except Exception:
        items = []
    after = search_stats()
    login_problem = after.get("login_wall", 0) > before.get("login_wall", 0)
    captcha = after.get("captcha", 0) > before.get("captcha", 0)
    timeline = after.get("timeline_responses", 0) > before.get("timeline_responses", 0)
    ok = bool(items or (timeline and not login_problem and not captcha))
    if not ok:
        _SEARCH_STATS["unavailable"] += 1
    return {"ok": ok, "items": len(items), "timeline_response": timeline,
            "login_wall": login_problem, "captcha": captcha}


def close_session():
    global _EXECUTOR
    with _EXEC_LOCK:
        ex = _EXECUTOR
    if ex is None:
        return
    def _close():
        if _SESSION[0] is not None:
            try:
                _SESSION[0].close()
            except Exception:
                pass
            _SESSION[0] = None
    try:
        ex.submit(_close).result(timeout=30)
    except Exception:
        pass


# --------------------------------------------------------------------------- CLI

if __name__ == "__main__":
    import sys
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "login").strip().lower()
    if cmd == "login":
        ok = login()
        print("LOGGED IN" if ok else "NOT LOGGED IN")
        sys.exit(0 if ok else 1)
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "logout":
        logout()
        print("logged out")
    elif cmd == "search":
        q = sys.argv[2] if len(sys.argv) > 2 else "満員電車"
        items = search_async(q, want=10, status_cb=print).result()
        for it in items[:10]:
            print("-", it["id"], "@" + it["author"]["uniqueId"], "|",
                  f"{it['stats']['diggCount']} likes |", it["desc"][:50])
        close_session()
    else:
        print("usage: python twitter_login.py [login|status|search <q>|logout]")
